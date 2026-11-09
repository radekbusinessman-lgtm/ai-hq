"""Projektový briefing – zadání pro agenta a vytvoření vstupní zprávy pro ChatService."""

from datetime import date

from app.agent.chat import InboundMessage
from app.db.enums import RunTrigger

BRIEFING_PROMPT = """\
[Automatický denní briefing – {day}]
Připrav stručný ranní briefing pro CEO. Postup:
1. list_projects – zjisti aktivní projekty.
2. Pro každý projekt list_tasks (otevřené) a search_facts (prázdný dotaz = nejnovější fakta).
Výstup česky, max. ~30 řádků, pro každý projekt v tomto pořadí:
1. **Aktuální stav projektu** – jen z uložených faktů (citace [F-…] a stav: ověřeno/předpoklad).
2. **Otevřené úkoly** (ref [T-…], priorita).
3. **Blížící se termíny** – po termínu a do 7 dnů.
4. **Rizika a nevyřešené otázky** – jen pokud plynou z dat; chybějící data výslovně uveď.
5. **Tři doporučené priority na dnešek.**
Nic si nedomýšlej; když data chybí, řekni to. Nic neukládej ani neměň.
"""


def briefing_message(
    *, day: date, channel_id: str, channel_name: str, model: str, manual_key: str | None = None
) -> InboundMessage:
    """Vstup pro ChatService. Klíč 'briefing:<den>' zajistí, že plánovaný briefing proběhne
    nejvýš jednou denně i po restartu. Ruční briefing (/briefing) má vlastní jedinečný klíč."""
    key = manual_key or f"briefing:{day.isoformat()}"
    return InboundMessage(
        channel="discord",
        channel_external_id=channel_id,
        channel_name=channel_name,
        message_external_id=key,
        author_external_id="scheduler",
        content=BRIEFING_PROMPT.format(day=day.strftime("%-d. %-m. %Y")),
        trigger=RunTrigger.MANUAL if manual_key else RunTrigger.SCHEDULE,
        model=model,
        actor="system:manual-briefing" if manual_key else "system:scheduler",
    )
