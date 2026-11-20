# -*- coding: utf-8 -*-
"""Lacak stored procedure penghasil notifikasi — membaca definisi, TANPA EXEC.

Pakai: berikan teks notifikasi. Skrip yang mencari SP-nya di seluruh database.

  python find_notif.py --text "Validasi gagal: area wajib diisi"

Cukup berikan teks notifikasi. Skrip mencari sendiri target koneksinya:

  1. qa-credentials.local.json / environment variable (lewat db_verify)
  2. connection string di file konfigurasi aplikasi di sekitar workspace
     (appsettings*.json, web.config, .env, docker-compose*.yml, dll.)
     — hanya nama server & database yang diambil, password TIDAK pernah
       dibaca/disimpan/ditampilkan
  3. instance SQL Server lokal yang umum
  4. Windows Integrated Auth dipakai bila tidak ada password → tidak perlu
     mengisi kredensial sama sekali

  python find_notif.py --text "Validasi gagal: area wajib diisi"

Bila perlu, target bisa ditentukan manual:
  python find_notif.py --text "..." --server "HOST\\SQLEXPRESS" --windows

Semua query adalah SELECT ke katalog sistem. Tidak ada EXEC, tidak ada DML/DDL.
"""
import os
import re
import sys
import json
import argparse
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db_verify as dv

LOCAL_INSTANCES = [r'(localdb)\MSSQLLocalDB', r'localhost\SQLEXPRESS',
                   'localhost', '127.0.0.1,1433']

CONFIG_NAMES = re.compile(
    r'(?i)^(appsettings.*\.json|web\.config|app\.config|'
    r'.*connectionstrings.*\.json|config\.json|\.env|'
    r'docker-compose.*\.ya?ml|.*\.config)$')
SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build'}
SERVER_RE = re.compile(r'(?i)\b(?:server|data source|address|addr|network address)\s*=\s*([^;"\']+)')
DB_RE = re.compile(r'(?i)\b(?:database|initial catalog)\s*=\s*([^;"\']+)')
JDBC_RE = re.compile(r'(?i)jdbc:sqlserver://([^;/"\']+?)(?::\d+)?(?:/[^;]*)?(?:;databaseName=([^;]+))?')

SEARCH_SQL = (
    "SELECT SCHEMA_NAME(o.schema_id) AS skema, o.name AS prosedur, "
    "m.definition AS definisi FROM sys.sql_modules m "
    "JOIN sys.objects o ON o.object_id = m.object_id "
    "WHERE o.type = 'P' AND m.definition LIKE ? ESCAPE '\\' "
    "ORDER BY SCHEMA_NAME(o.schema_id), o.name"
)
DB_SQL = "SELECT name FROM sys.databases WHERE state_desc = 'ONLINE' AND name <> 'tempdb'"


def _connect_windows(server, database):
    import pyodbc
    driver = dv._cred('driver') or 'ODBC Driver 18 for SQL Server'
    parts = ['DRIVER={' + driver + '}', 'SERVER=' + server,
             'DATABASE=' + database, 'Trusted_Connection=yes']
    if driver.startswith('ODBC Driver 1'):
        parts.append('TrustServerCertificate=yes')
    return pyodbc.connect(';'.join(parts), timeout=10)


def _connect_ctx(server, database, windows):
    """Koneksi read-only. Windows auth tidak butuh password."""
    if windows or not dv._cred('password'):
        return _connect_windows(server, database), 'windows'
    os.environ['QA_DB_SERVER'] = server
    os.environ['QA_DB_NAME'] = database
    return dv._connect(), 'sql'


def _clean(value):
    if not value:
        return ''
    v = str(value).strip().strip('"\'')
    if v.lower().startswith('tcp:'):
        v = v[4:]
    return v.split(',')[0].strip() if v.count(',') == 1 else v.strip()


def discover_targets(roots=None, max_depth=3, max_files=150):
    """Cari nama server/database dari file konfigurasi di sekitar workspace.

    Hanya nama server & database yang diekstrak. Baris password/user id sengaja
    diabaikan — tidak pernah masuk ke variabel, JSON output, atau layar.
    """
    roots = roots or [os.getcwd(),
                      os.path.dirname(os.path.dirname(os.path.dirname(
                          os.path.dirname(os.path.abspath(__file__))))),
                      os.path.join(os.path.expanduser('~'), '.config', 'opencode')]
    seen, found, scanned = set(), [], 0
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.')][:]
            depth = dirpath[len(root):].count(os.sep)
            if depth >= max_depth:
                dirnames[:] = []
            for name in filenames:
                if not CONFIG_NAMES.match(name):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
                        content = fh.read(200000)
                except OSError:
                    continue
                scanned += 1
                if scanned > max_files:
                    return found
                pairs = []
                sm, dm = SERVER_RE.search(content), DB_RE.search(content)
                if sm:
                    pairs.append((_clean(sm.group(1)), _clean(dm.group(1)) if dm else ''))
                for jm in JDBC_RE.finditer(content):
                    pairs.append((_clean(jm.group(1)), _clean(jm.group(2))))
                for srv, dbname in pairs:
                    srv = srv.strip()
                    if not srv or len(srv) > 120 or '${' in srv:
                        continue
                    key = (srv.lower(), dbname.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append({'server': srv, 'database': dbname,
                                  'source': os.path.relpath(path, os.getcwd())})
    return found


def search_definitions(conn, text, limit=20):
    """Cari teks notifikasi di definisi seluruh SP. Kembalikan daftar cocok."""
    cur = conn.cursor()
    cur.execute(SEARCH_SQL, ['%' + dv._esc_like(text) + '%'])
    cols = [d[0] for d in (cur.description or [])]
    found = []
    for row in cur.fetchmany(limit):
        rec = dict(zip(cols, row))
        definition = str(rec.get('definisi') or '')
        hits = [{'line': i + 1, 'content': ln.strip()}
                for i, ln in enumerate(definition.splitlines())
                if text.lower() in ln.lower()]
        found.append({
            'schema': rec.get('skema'),
            'procedure': rec.get('prosedur'),
            'matching_lines': hits,
            'definition': definition,
        })
    return found


def find(text, server=None, database=None, windows=False, limit=20, out=None, discover=True):
    tried, results, used = [], [], []
    discovered = (discover_targets() if discover and not server and not dv._cred('server') else [])

    if server:
        pairs = [(server, database or '')]
    elif dv._cred('server'):
        pairs = [(dv._cred('server'), database or dv._cred('database') or '')]
    elif discovered:
        pairs = [(d['server'], database or d['database']) for d in discovered]
    else:
        pairs = [(s, database or '') for s in LOCAL_INSTANCES]
    if not server and not dv._cred('server'):
        existing = {p[0].lower() for p in pairs}
        pairs += [(s, database or '') for s in LOCAL_INSTANCES if s.lower() not in existing]

    for srv, dbname in pairs:
        probe_db = dbname or 'master'
        try:
            conn, auth = _connect_ctx(srv, probe_db, windows)
        except Exception as ex:
            tried.append({'server': srv, 'database': probe_db, 'error': str(ex)[:180]})
            continue
        source = next((d['source'] for d in discovered
                       if d['server'].lower() == srv.lower()), None)
        try:
            targets = [dbname] if dbname else None
            if targets is None:
                cur = conn.cursor()
                cur.execute(DB_SQL)
                targets = [r[0] for r in cur.fetchall()]
            for name in targets:
                dbconn = conn
                if name != probe_db:
                    try:
                        dbconn, _ = _connect_ctx(srv, name, windows)
                    except Exception as ex:
                        tried.append({'server': srv, 'database': name, 'error': str(ex)[:180]})
                        continue
                try:
                    matches = search_definitions(dbconn, text, limit)
                    used.append({'server': srv, 'database': name, 'auth': auth,
                                 'source': source})
                    for m in matches:
                        m['database'] = name
                        results.append(m)
                finally:
                    if dbconn is not conn:
                        dbconn.close()
        finally:
            conn.close()
        if results:
            break

    payload = {
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'notification_text': text,
        'mode': 'baca definisi SP via SELECT katalog — tanpa EXEC / DML / DDL',
        'auto_discovered_from': [d['source'] for d in discovered],
        'connection': used,
        'status': 'FOUND' if results else ('BLOCKED' if not used else 'NOT_FOUND'),
        'matches': results,
        'tried': tried,
    }
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    return payload


def main():
    ap = argparse.ArgumentParser(description='Lacak SP penghasil notifikasi (read-only, tanpa EXEC)')
    ap.add_argument('--text', required=True, help='teks notifikasi yang ingin dilacak')
    ap.add_argument('--server', help='nama instance SQL Server (opsional, tidak disimpan)')
    ap.add_argument('--database', help='nama database (opsional; default: semua database online)')
    ap.add_argument('--windows', action='store_true', help='Windows Integrated Auth — tanpa password')
    ap.add_argument('--limit', type=int, default=20)
    ap.add_argument('--out', default=os.path.join('pendukung', 'ai', 'sp-notif-search.json'))
    ap.add_argument('--no-discover', action='store_true',
                    help='jangan baca file konfigurasi untuk mencari target')
    a = ap.parse_args()

    r = find(a.text, a.server, a.database, a.windows, a.limit, a.out,
             discover=not a.no_discover)
    print('STATUS', r['status'], '| teks:', r['notification_text'])
    if r['auto_discovered_from']:
        print('TARGET DITEMUKAN DI', r['auto_discovered_from'])
    print('KONEKSI', r['connection'] or '-')
    for m in r['matches']:
        print('  SP', m['database'] + '.' + m['schema'] + '.' + m['procedure'])
        for h in m['matching_lines']:
            print('     baris', h['line'], ':', h['content'][:150])
    if r['status'] == 'BLOCKED':
        print('  Tidak bisa konek. Percobaan:')
        for t in r['tried']:
            print('   ', t)
    print('OUT', a.out)


if __name__ == '__main__':
    main()
