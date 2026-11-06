# 0002 – Hosting: domácí Mac mini jako server, databáze v Supabase (Frankfurt)

**Datum:** 2026-11-06 · **Stav:** navrženo, čeká na potvrzení

## Kontext
Systém musí běžet nepřetržitě i bez zapnutého pracovního počítače. Uživatel má doma
Mac mini s 2TB diskem. Schválen byl Hetzner VPS + Supabase.

## Rozhodnutí
Pro fázi MVP poběží aplikace na Mac mini (Docker), databáze zůstává v Supabase Frankfurt.
Hetzner VPS zůstává připravený jako záložní / budoucí cíl – stejný `docker compose`.

## Proč to jde bezpečně
Discord bot i Claude API používají pouze **odchozí** spojení. Na routeru se neotevírá
žádný port a Mac mini není z internetu dosažitelný. API aplikace naslouchá jen na localhostu.

## Rizika a opatření
| Riziko | Opatření |
|---|---|
| Výpadek proudu / internetu doma | Data jsou v Supabase, nic se neztratí; bot po obnovení naběhne sám. Volitelně malá UPS. |
| Mac usne / restart po aktualizaci macOS | Vypnout uspávání, zapnout „Spustit po výpadku napájení“, Docker se startem systému. |
| Selhání disku / krádež Macu | Paměť v Supabase (automatické zálohy). Originály dokumentů na 2TB disku + noční kopie do cloudu v EU. FileVault zapnutý. |
| Sdílení Macu s osobním používáním | Samostatný uživatelský účet macOS pro AI HQ. |

## Důsledky
- (+) Nulové náklady na server, 2TB pro dokumenty, data fyzicky u vás.
- (−) Dostupnost závisí na domácím proudu a internetu (pro MVP přijatelné).
