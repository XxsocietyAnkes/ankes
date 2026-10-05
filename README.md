# ANKES V3 — Ready Deploy

## Fitur
- Panel `/settings` inline
- Anti-GCast/forward
- Anti-link + custom allowed domains
- Optional Telegram/YouTube/Instagram link allow
- Anti-flood dengan limit/window configurable
- Flood action: delete/mute/ban
- Anti-mention dengan limit
- Anti-spam + maximum message length
- Strike system
- Strike action: delete/mute/ban
- Whitelist user
- Blacklist user
- Blacklist text
- Welcome
- Manual mute/ban/warn
- Action logs di PostgreSQL
- Optional LOG_CHAT_ID
- Pengaturan per grup

## 1. BotFather
Buat bot dengan `/newbot`.
Atur `/setprivacy` -> `Disable` agar bot dapat membaca pesan biasa di grup.

## 2. Hak admin
Bot harus menjadi admin dengan izin:
- Delete Messages
- Restrict Members
- Ban Users
- Invite Users bila ingin mengelola invite/welcome lebih lanjut

## 3. Environment
`BOT_TOKEN` = token BotFather.
`DATABASE_URL` = PostgreSQL connection string.
`LOG_CHAT_ID` = opsional, chat ID tempat log dikirim.

## 4. Deploy Koyeb
Repository GitHub:
- Build command: `pip install -r requirements.txt`
- Run command: `python bot.py`
- Service type: Worker
- Environment variables: BOT_TOKEN, DATABASE_URL, LOG_CHAT_ID (opsional)

## 5. Command konfigurasi

### Panel
`/settings`

### Flood
`/setflood 5 8`
`/floodaction delete`
`/floodaction mute`
`/floodaction ban`
`/setmute 300`

### Strike
`/setstrike 3`
`/strikeaction delete`
`/strikeaction mute`
`/strikeaction ban`

### Users
`/whitelist 123`
`/unwhitelist 123`
`/blacklist 123`
`/unblacklist 123`

### Text
`/bltext kata`
`/unbltext kata`
`/bltextlist`

### Link allowlist
`/allowdomain example.com`
`/delallowdomain example.com`
`/allowlist`

### Moderasi manual
Reply pesan user:
`/mute`
`/mute 600`
`/ban`
`/warn`
`/clearstrike`

## Catatan GCast
Telegram tidak menyediakan flag universal bernama "GCast". Bot mendeteksi metadata forward. GCast yang mengirim pesan tanpa metadata forward memerlukan metode deteksi khusus.

## Keamanan
Jangan memasukkan BOT_TOKEN atau DATABASE_URL ke GitHub.
Gunakan Environment Variables.

## PostgreSQL
Schema dibuat otomatis saat bot start.
Pengaturan setiap grup tersimpan di database sehingga tidak hilang karena restart aplikasi, selama database PostgreSQL tetap tersedia.
