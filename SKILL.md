---
name: sp-tracer
description: 'Lacak stored procedure penghasil notifikasi validasi dari teks notifikasi saja — baca definisi lewat SELECT katalog, tanpa EXEC, tanpa mengisi kredensial. Pakai saat ada toast/error validasi dan ingin tahu SP mana pengirimnya, field apa yang wajib diisi, serta aturan apa di dalam prosedur. Juga dipakai untuk verifikasi database read-only.'
---

# Skill: SP Tracer

Lacak **notifikasi / pesan validasi → stored procedure pengirimnya**. Yang dibaca adalah **teks definisi** prosedur, bukan eksekusinya.

Bisa dipakai kapan saja — tidak harus sedang menjalankan E2E atau bug hunt.

## Kapan dipakai

- Ada toast / notifikasi / pesan validasi, tetapi tidak tahu SP mana yang mengirim.
- Ingin tahu field mana yang wajib diisi dan aturan apa yang ada di dalam SP.
- Ingin membuktikan ada/tidaknya kondisi tertentu di definisi SP tanpa menjalankannya.
- Perlu verifikasi database read-only sebagai bukti tambahan.

## Cara memanggil

| Cara | Contoh |
|---|---|
| Chat, eksplisit | `@sp-tracer` lalu `cari notifikasi: "Validasi gagal: area wajib diisi"` |
| Chat, otomatis | `trace notif "Validasi gagal: area wajib diisi"` — skill dimuat dari deskripsinya |
| Terminal | `python C:\Users\Harrys\.config\opencode\skills\sp-tracer\find_notif.py --text "Validasi gagal: area wajib diisi"` |

Opsi `find_notif.py`: `--text` (wajib), `--server`, `--database`, `--windows`, `--limit`, `--out`, `--no-discover`.

## Alur kerja

1. **Minta teks notifikasinya.** Jangan pernah minta password.
2. **Jalankan pelacakan:**
   `python find_notif.py --text "<teks notifikasi>"`
   Target koneksi dicari sendiri, berurutan: file kredensial lokal → connection string di `appsettings*.json` / `web.config` / `.env` / `docker-compose*.yml` di sekitar workspace → instance SQL Server lokal. Windows Integrated Auth dipakai bila tidak ada password, jadi **tidak perlu mengisi kredensial**.
3. **Baca hasil** di `pendukung/ai/sp-notif-search.json`:
   - `status`: `FOUND` / `NOT_FOUND` / `BLOCKED`
   - `connection`: server, database, auth, dan sumber target ditemukan
   - `matches[].database.schema.prosedur`, `matching_lines` (nomor baris + isi), dan `definition` lengkap
4. **Jelaskan temuan:** SP pengirim, field yang diminta, aturan di dalam prosedur, dan kutip baris definisi sebagai bukti.
5. **Bila hasil dipakai dalam DOCX:** teruskan ringkasan ke bab `db_verification` lewat skill `qa-e2e-tester` atau `bug-hunter` (`"db_verification"` di `report.chapters`). Skill ini tidak menggantikan generator laporan.

## Banyak SP dalam satu database

`find_notif.py` mencari **langsung dari isi definisi**, jadi tidak perlu tahu nama SP lebih dulu. Ia menyaring seluruh prosedur di semua database online, bukan menebak berdasarkan nama.

Untuk verifikasi yang lebih terarah, pakai `db_verify.py` dengan kunci `sp`:

```json
[
  {"id":"SP-01", "sp":"dbo.ValidateProblem",
   "expect":{"column":"definisi","contains_all":["IF @area","RAISERROR"]}},
  {"id":"SP-02", "sp":"Valid*", "limit":10, "offset":0},
  {"id":"SP-03", "sp":"*", "definition_contains":"area wajib diisi"},
  {"id":"SP-04", "sp":"*", "definition_all":["IF @status","RAISERROR"],
   "limit":20, "offset":20}
]
```

- `sp`: nama exact, pola `*` (mis. `Valid*`), `schema.nama`, atau `*` untuk semua.
- `definition_contains` / `definition_all`: cari teks di dalam definisi SP.
- `offset` + `limit`: paging; hasil menyimpan `total_matching`.
- Pencarian definisi yang tidak cocok dilaporkan `FAIL` dengan alasan, bukan dibungkus "lolos".

Jalankan: `python db_verify.py --checks <file-json> --out pendukung/ai/db-verification.json`.

## Guardrail — tidak bisa ditawar

- **Hanya `SELECT` / `WITH` / `PRAGMA`**, satu statement. `EXEC`, `INSERT`, `UPDATE`, `DELETE`, `DROP`, dan multi-statement ditolak.
- Prosedur **tidak pernah dijalankan**. Di DOCX diberi label "SP · definisi" dan catatan "prosedur TIDAK dijalankan (tanpa EXEC)".
- **Password tidak pernah diminta di chat**, tidak ditulis di spec, DOCX, atau JSON hasil. Dari file konfigurasi aplikasi hanya nama server dan database yang diekstrak.
- Database tidak terjangkau → **`BLOCKED`**. Jangan mengarang hasil. Yang diminta adalah nama instance / akses jaringan, bukan password.
- SQLite tidak punya stored procedure; check `sp` di SQLite gagal dengan pesan jelas, bukan diklaim lolos.
- Data pelanggan yang muncul di hasil query tunduk pada aturan pelaporan skill laporan — jangan disertakan mentah di laporan yang dibagikan.

## Kredensial

Prioritas: `QA_CREDENTIALS_FILE` → `%USERPROFILE%\.config\opencode\qa-credentials.local.json` → `qa-credentials.local.json` di folder skill ini. Environment variable `QA_DB_*` lebih prioritas dari file.

Template kosong ada di `qa-credentials.example.json`. Salin ke `qa-credentials.local.json` lalu isi bila perlu; nama `.local.json` sudah masuk `.gitignore`, jadi tidak bisa ikut ter-commit.
