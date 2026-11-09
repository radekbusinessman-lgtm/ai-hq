# 0003 – Oddělení firem v nástrojích, /stop s přerušením, historie úkolů

**Datum:** 2026-11-09 · **Stav:** implementováno (verze 0.7)

## Oddělení firem
- V kanálu projektu (konverzace má `company_id`) nástroje vidí jen data téže firmy:
  `resolve_project`, `list_projects`, `list_tasks`, `find_fact` / `_find_task` (podle ref).
  Pokus o projekt jiné firmy vrátí chybu s odkazem na #chief-of-staff.
- Obecný kanál (#chief-of-staff, bez projektu) vidí všechny firmy – vlastník je jeden.
- Ve schématu nese každý záznam `company_id`; RLS (migrace 0003) chrání před veřejným API.

## /stop
1. Uloží vypínač `agents_enabled = false` (od té chvíle nenaběhne žádný nový běh ani
   plánovaná úloha).
2. Přeruší běžící běhy agenta (`ChatService.cancel_running`); běh dostane stav `cancelled`,
   rozpracované transakce se vrátí zpět.
3. Zprávy se dál ukládají. Zapnutí jen `/start` vlastníkem.
Schválené akce (approvals) se nepřerušují – jsou krátké a atomické.

## Historie úkolů
Auditní log (append-only) ukládá u založení hodnoty a u změn dvojice [původní, nová].
Nástroj `task_history` ji zobrazí.
