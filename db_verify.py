# -*- coding: utf-8 -*-
"""Verifikasi database READ-ONLY untuk QA E2E Tester.

Kredensial dibaca dari file lokal DI LUAR repo git:
  %USERPROFILE%\\.config\\opencode\\qa-credentials.local.json
(bisa diubah lewat QA_CREDENTIALS_FILE). Environment variable lebih prioritas.
Kredensial tidak pernah ditulis ke argumen, chat, spec, atau laporan:

  QA_DB_SQLITE   path file SQLite (mode tanpa setup sama sekali)
  QA_DB_SERVER   SQL Server: "host\\instance" atau "host,port"
  QA_DB_NAME     nama database
  QA_DB_USER     username (SQL Server authentication)
  QA_DB_PASS     password (SQL Server authentication)
  QA_DB_AUTH     "sql" (default) atau "windows"
  QA_DB_DRIVER   override nama ODBC driver
  QA_DB_LIMIT    maksimum baris yang diambil per query (default 20)

File checks berisi daftar asersi SELECT. Contoh:

  [
    {"id": "DB-01", "name": "Status problem setelah Submit",
     "sql": "SELECT id, status FROM Problem WHERE id = ?",
     "params": [8200782436],
     "expect": {"column": "status", "value": "OPEN"}},
    {"id": "DB-02", "name": "Baris audit terisi",
     "sql": "SELECT COUNT(*) AS n FROM Audit WHERE created_at IS NOT NULL",
     "expect": {"column": "n", "min": 1}}
  ]

Expect yang didukung: `value`, `min`, `max`, `contains`, `contains_all`,
`row_count`, `row_count_min`, `row_count_max`, `exists`.

`contains_all` berguna untuk SP: mengecek beberapa kondisi sekaligus di teks
definisi, misalnya `["IF @status", "RAISERROR", "RETURN"]`.

Stored procedure (SQL Server) — MEMBACA DEFINISI, bukan mengeksekusi:

  {"id": "SP-01", "sp": "dbo.NamaProsedur",
   "expect": {"column": "definisi", "contains_all": ["IF @status","RAISERROR"]}}
  {"id": "SP-02", "sp": "Valid*", "limit": 10, "offset": 0}
  {"id": "SP-03", "sp": "*", "definition_contains": "area wajib diisi"}
  {"id": "SP-04", "sp": "*", "definition_all": ["IF @status","RAISERROR"],
   "limit": 20, "offset": 20}

`sp`: nama exact, pola `*` (mis. `Valid*`), `schema.nama`, atau `*` untuk semua.
`definition_contains` / `definition_all` mencari teks di dalam definisi SP —
cocok untuk melacak notifikasi validasi ke prosedur pengirimnya.
`offset` + `limit` melakukan paging; hasil menyimpan `total_matching`.
Kunci `sp` menghasilkan SELECT ke `sys.sql_modules` / `sys.objects` dengan
parameter binding — tetap tunduk pada guard read-only. `EXEC` sengaja tidak
didukung: isi prosedur bisa saja mengubah data.

Jalankan:
  python db_verify.py --checks checks.json --out pendukung/ai/db-verification.json
"""
import os
import json
import argparse
import datetime
import sqlite3

ALLOWED_PREFIX = ('select', 'with', 'pragma')
STATUS_PASS, STATUS_FAIL, STATUS_ERROR = 'PASS', 'FAIL', 'ERROR'

_USER_CRED = os.path.join(os.path.expanduser('~'), '.config', 'opencode', 'qa-credentials.local.json')
_LOCAL_CRED = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'qa-credentials.local.json')


def _resolve_cred_file():
    """Prioritas: env → file user-level → file di folder skill (hasil clone)."""
    explicit = os.environ.get('QA_CREDENTIALS_FILE')
    if explicit:
        return explicit
    if os.path.isfile(_USER_CRED):
        return _USER_CRED
    if os.path.isfile(_LOCAL_CRED):
        return _LOCAL_CRED
    return _USER_CRED


CRED_FILE = _resolve_cred_file()
ENV_MAP = {
    'sqlite': 'QA_DB_SQLITE', 'server': 'QA_DB_SERVER', 'database': 'QA_DB_NAME',
    'user': 'QA_DB_USER', 'password': 'QA_DB_PASS', 'auth': 'QA_DB_AUTH',
    'driver': 'QA_DB_DRIVER', 'limit': 'QA_DB_LIMIT',
}
_CREDS = None


def _cred(key, default=''):
    """Environment variable menang; kalau kosong, baca dari file kredensial lokal."""
    global _CREDS
    val = os.environ.get(ENV_MAP.get(key, ''))
    if val:
        return val
    if _CREDS is None:
        try:
            with open(CRED_FILE, encoding='utf-8') as fh:
                _CREDS = (json.load(fh).get('db') or {})
        except Exception:
            _CREDS = {}
    val = _CREDS.get(key, default)
    return default if val is None else val


def _safe_sql(sql):
    """Terima hanya satu pernyataan baca. Tolak DML/DDL dan multi-statement."""
    s = str(sql or '').strip()
    if not s:
        raise ValueError('query kosong')
    if s.rstrip(';').count(';') > 0:
        raise ValueError('multi-statement tidak diizinkan')
    if not s.lower().startswith(ALLOWED_PREFIX):
        raise ValueError('hanya SELECT / WITH / PRAGMA yang diizinkan')
    return s


# Katalog SQL Server — membaca teks definisi SP. Bukan EXEC.
_SP_COLS = ("SELECT SCHEMA_NAME(o.schema_id) AS skema, o.name AS prosedur, "
            "m.definition AS definisi FROM sys.sql_modules m "
            "JOIN sys.objects o ON o.object_id = m.object_id WHERE ")
_SP_COUNT = ("SELECT COUNT(*) AS jumlah FROM sys.sql_modules m "
             "JOIN sys.objects o ON o.object_id = m.object_id WHERE ")
SP_LIST_SQL = _SP_COLS + "o.type = 'P' ORDER BY SCHEMA_NAME(o.schema_id), o.name"
SP_ONE_SQL = _SP_COLS + "o.type = 'P' AND o.name = ?"
SP_SCHEMA_SQL = _SP_COLS + "o.type = 'P' AND SCHEMA_NAME(o.schema_id) = ? AND o.name = ?"


def _esc_like(val):
    """Escape wildcard LIKE; semua nilai tetap dikirim sebagai parameter binding."""
    return str(val).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _prepare_check(chk):
    """Ubah check bertipe `sp` menjadi SELECT katalog yang aman.

    MEMBACA DEFINISI stored procedure, bukan menjalankannya.
    - `sp`: nama exact, pola memakai `*`, `schema.nama`, atau `*` untuk semua.
    - `definition_contains` / `definition_all`: cari teks di dalam definisi SP.
    - `offset` / `limit`: paging kalau database punya banyak SP.
    """
    chk = dict(chk)
    sp = chk.pop('sp', None)
    if sp in (None, ''):
        return chk
    raw = str(sp).strip()
    chk['kind'] = 'sp_definition'
    where = ["o.type = 'P'"]
    params = []
    exact = False

    def _name_clause(value):
        nonlocal exact
        if '*' in value:
            where.append("o.name LIKE ? ESCAPE '\\'")
            params.append(_esc_like(value).replace('*', '%'))
        else:
            where.append("o.name = ?")
            params.append(value)
            exact = True

    if raw == '*':
        pass
    elif '.' in raw:
        schema, name = raw.split('.', 1)
        where.append("SCHEMA_NAME(o.schema_id) = ?")
        params.append(schema.strip())
        _name_clause(name.strip())
    else:
        _name_clause(raw)

    terms = chk.pop('definition_all', None)
    if terms is None:
        one = chk.pop('definition_contains', None)
        terms = [one] if one else []
    if isinstance(terms, str):
        terms = [terms]
    chk['_definition_terms'] = list(terms)
    for t in terms:
        where.append("m.definition LIKE ? ESCAPE '\\'")
        params.append('%' + _esc_like(t) + '%')

    clause = ' AND '.join(where)
    chk['_where'] = clause
    chk['_params'] = list(params)
    chk['sql'] = _SP_COLS + clause + " ORDER BY SCHEMA_NAME(o.schema_id), o.name"
    chk['params'] = list(params)
    chk.setdefault('name', ('Definisi SP ' + raw) if exact else 'Pencarian SP ' + raw)
    return chk


def _connect():
    sqlite_path = _cred('sqlite')
    if sqlite_path:
        if not os.path.isfile(sqlite_path):
            raise RuntimeError('File SQLite tidak ditemukan: ' + sqlite_path)
        uri = 'file:' + sqlite_path.replace('\\', '/') + '?mode=ro'
        return sqlite3.connect(uri, uri=True), {'engine': 'SQLite', 'target': os.path.basename(sqlite_path)}

    try:
        import pyodbc
    except ImportError:
        raise RuntimeError('pyodbc belum terpasang: python -m pip install pyodbc')

    server = _cred('server')
    database = _cred('database')
    if not server or not database:
        raise RuntimeError('Isi server dan database di ' + CRED_FILE)

    driver = _cred('driver') or 'ODBC Driver 18 for SQL Server'
    parts = [
        'DRIVER={' + driver + '}',
        'SERVER=' + server,
        'DATABASE=' + database,
    ]
    if _cred('auth', 'sql').lower() == 'windows':
        parts.append('Trusted_Connection=yes')
    else:
        parts.append('UID=' + str(_cred('user')))
        parts.append('PWD=' + str(_cred('password')))
    if driver.startswith('ODBC Driver 1'):
        parts.append('TrustServerCertificate=yes')
    conn = pyodbc.connect(';'.join(parts), timeout=15, attributes_before_connect=None)
    return conn, {'engine': 'SQL Server', 'target': database, 'driver': driver}


def _evaluate(check, columns, rows):
    """Kembalikan (status, keterangan) berdasarkan blok `expect`."""
    exp = check.get('expect') or {}
    if not exp:
        return STATUS_PASS, 'Query berhasil; tidak ada asersi nilai'
    actual_rows = len(rows)

    if 'exists' in exp:
        ok = (actual_rows > 0) == bool(exp['exists'])
        return (STATUS_PASS if ok else STATUS_FAIL), f'exists={actual_rows > 0} (expected {bool(exp["exists"])})'

    for key in ('row_count', 'row_count_min', 'row_count_max'):
        if key in exp:
            want = int(exp[key])
            ok = {'row_count': actual_rows == want,
                  'row_count_min': actual_rows >= want,
                  'row_count_max': actual_rows <= want}[key]
            return (STATUS_PASS if ok else STATUS_FAIL), f'row_count={actual_rows} ({key}={want})'

    col = exp.get('column')
    if col and col in columns and rows:
        idx = columns.index(col)
        val = rows[0][idx]
        if 'value' in exp:
            ok = str(val) == str(exp['value'])
        elif 'min' in exp:
            ok = val is not None and float(val) >= float(exp['min'])
        elif 'max' in exp:
            ok = val is not None and float(val) <= float(exp['max'])
        elif 'contains' in exp:
            ok = str(exp['contains']) in str(val)
        elif 'contains_all' in exp:
            missing = [s for s in exp['contains_all'] if str(s) not in str(val)]
            return (STATUS_PASS if not missing else STATUS_FAIL), (
                f'{col}: semua kondisi ditemukan' if not missing
                else f'{col}: kondisi tidak ditemukan {missing}')
        else:
            return STATUS_PASS, f'{col}={val}'
        return (STATUS_PASS if ok else STATUS_FAIL), f'{col}={val} (expected {exp})'

    return STATUS_FAIL, f'Kolom asersi tidak ditemukan: {col} (kolom tersedia: {columns})'


def run_checks(checks_path, out_path, limit=20):
    with open(checks_path, encoding='utf-8') as fh:
        checks = json.load(fh)
    if isinstance(checks, dict):
        checks = checks.get('checks', [])

    meta = {}
    results = []
    conn = None
    try:
        conn, meta = _connect()
        for raw_chk in checks:
            chk = _prepare_check(raw_chk)
            item = {'id': chk.get('id', '-'), 'name': chk.get('name', chk.get('id', '-')),
                    'kind': chk.get('kind', 'query'),
                    'sql': str(chk.get('sql', '')), 'status': STATUS_ERROR,
                    'detail': '', 'columns': [], 'rows': [], 'error': ''}
            try:
                sql = _safe_sql(chk.get('sql'))
                want = max(1, int(chk.get('limit') or limit))
                off = max(0, int(chk.get('offset') or 0))
                cur = conn.cursor()
                cur.execute(sql, tuple(chk.get('params') or []))
                columns = [d[0] for d in (cur.description or [])]
                fetched = [list(r) for r in cur.fetchmany(off + want)]
                rows = fetched[off:]
                item['columns'] = columns
                item['rows'] = rows
                if chk.get('kind') == 'sp_definition':
                    item['offset'], item['page_limit'] = off, want
                    try:
                        ccur = conn.cursor()
                        ccur.execute(_SP_COUNT + chk['_where'], tuple(chk['_params']))
                        crow = ccur.fetchone()
                        if crow:
                            item['total_matching'] = int(crow[0])
                    except Exception:
                        pass
                status, detail = _evaluate(chk, columns, rows)
                if chk.get('_definition_terms') and not rows and status == STATUS_PASS:
                    status = STATUS_FAIL
                    detail = ('Tidak ada SP yang definisinya mengandung: '
                              + ', '.join(chk['_definition_terms']))
                item['status'], item['detail'] = status, detail
            except Exception as ex:
                item['error'] = str(ex)
                if chk.get('kind') == 'sp_definition' and meta.get('engine') == 'SQLite':
                    item['detail'] = 'SQLite tidak memiliki stored procedure; kunci `sp` hanya untuk SQL Server.'
                else:
                    item['detail'] = 'Query ditolak atau gagal: ' + str(ex)
            results.append(item)
    except Exception as ex:
        for raw_chk in checks:
            chk = _prepare_check(raw_chk)
            results.append({'id': chk.get('id', '-'), 'name': chk.get('name', chk.get('id', '-')),
                            'kind': chk.get('kind', 'query'),
                            'sql': str(chk.get('sql', '')), 'status': STATUS_ERROR,
                            'detail': 'Koneksi database gagal: ' + str(ex),
                            'columns': [], 'rows': [], 'error': str(ex)})
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    payload = {
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'engine': meta.get('engine', 'tidak tersambung'),
        'target': meta.get('target', '-'),
        'driver': meta.get('driver', '-'),
        'read_only': True,
        'mode': 'SELECT / WITH / PRAGMA saja; tidak ada DML/DDL/EXEC. SP hanya dibaca definisinya.',
        'summary': {
            'total': len(results),
            'pass': sum(1 for r in results if r['status'] == STATUS_PASS),
            'fail': sum(1 for r in results if r['status'] == STATUS_FAIL),
            'error': sum(1 for r in results if r['status'] == STATUS_ERROR),
        },
        'checks': results,
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print('DB VERIFY', payload['engine'], payload['target'], payload['summary'], '->', out_path)
    return payload


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Verifikasi database read-only untuk QA E2E')
    ap.add_argument('--checks', required=True, help='file JSON berisi daftar query asersi')
    ap.add_argument('--out', default=os.path.join('pendukung', 'ai', 'db-verification.json'))
    ap.add_argument('--limit', type=int, default=int(_cred('limit', 20)))
    a = ap.parse_args()
    run_checks(a.checks, a.out, a.limit)
