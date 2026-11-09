# 0004 – Pošta: iCloud Mail přes IMAP, jen čtení

**Datum:** 2026-11-09 · **Stav:** implementováno (verze 0.8), živé připojení čeká na bezpečnostní předpoklad

## Kontext
Pracovní pošta je radyn@me.com (iCloud), kalendář Apple. Gmail se prakticky nepoužívá.
Apple pro poštu nenabízí OAuth API; oficiální přístup = IMAP/SMTP s heslem pro aplikaci.

## Rozhodnutí
- IMAP `imap.mail.me.com:993` (TLS), heslo pro aplikaci v `.env` (`ICLOUD_APP_PASSWORD`).
- Jen čtení: `select(readonly=True)` (EXAMINE), `BODY.PEEK` (neoznačí přečtené), pouze příkazy
  SEARCH a FETCH (ověřeno testem). SMTP/odesílání v kódu neexistuje.
- Nástroje: `mail_search`, `mail_read`, `mail_thread`, `mail_waiting_for_reply` (úroveň 1).
- Prompt injection: nástroje pošty označí běh (`tainted_by`); takový běh nesmí navrhovat akce
  ke schválení. Výstupy pošty se neukládají do `agent_runs.tool_calls`.
- Návrhy odpovědí jen jako text v Discordu. Odesílání přijde později přes schvalování
  (konkrétní příjemce, text, přílohy).

## Bezpečnostní předpoklad
Heslo pro aplikaci dává přístup k celé iCloud poště, kontaktům i kalendáři. Uložení na disk
Macu vyžaduje zapnutý FileVault. Zrušení: account.apple.com → Hesla pro aplikace.
