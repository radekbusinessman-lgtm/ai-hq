# 0001 – Vlastní minimalistický orchestrátor místo OpenClaw

**Datum:** 2026-11-06 · **Stav:** schváleno (Radoslav Bodeček)

## Kontext
Potřebujeme AI Chief of Staff s přístupem k citlivým firemním datům (e-maily, smlouvy,
projektová dokumentace), s pamětí a s řízenými oprávněními.

## Zvažované varianty
- **A: OpenClaw** – hotový agent s integracemi. K 10/2026: 14 kritických bezpečnostních
  upozornění v 1–4/2026 (RCE, únik ze sandboxu), stovky škodlivých skills v ClawHubu,
  prompt injection upstream výslovně mimo rozsah, tajemství výchozí v plaintextu.
- **B: Vlastní orchestrátor** – Python + Claude API, nástroje definované v kódu.

## Rozhodnutí
Varianta B. Agent umí pouze nástroje, které explicitně naprogramujeme, s úrovní oprávnění
v kódu. Žádný shell, žádné cizí pluginy. Paměť v naší databázi, nezávislá na modelu.

## Důsledky
- (+) Plná kontrola nad daty, oprávněními a auditem; malá útočná plocha.
- (−) Integrace (Gmail, Drive, Kalendář) píšeme sami přes oficiální API.
