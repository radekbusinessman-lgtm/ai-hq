"""Nástroje pro interní úkoly (úroveň 1 – interní evidence, nic neodchází ven)."""

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from app.audit import audit
from app.db.enums import PermissionLevel, TaskStatus
from app.db.models import AuditLog, Project, Task
from app.tools.memory import PROJECT_PARAM, resolve_project
from app.tools.registry import Tool, ToolContext, ToolInputError

MAX_LIST = 50
PRIORITY_LABEL = {1: "vysoká", 2: "střední", 3: "nízká"}


def task_ref(task_id: Any) -> str:
    return "T-" + str(task_id).replace("-", "")[:8]


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as e:
        raise ToolInputError(f"Neplatné datum '{value}', použij RRRR-MM-DD.") from e


def _parse_priority(value: Any) -> int:
    if value in (None, ""):
        return 2
    try:
        p = int(value)
    except (TypeError, ValueError) as e:
        raise ToolInputError("Priorita musí být 1 (vysoká), 2 (střední) nebo 3 (nízká).") from e
    if p not in PRIORITY_LABEL:
        raise ToolInputError("Priorita musí být 1 (vysoká), 2 (střední) nebo 3 (nízká).")
    return p


def _task_dict(t: Task, project_name: str | None) -> dict[str, Any]:
    overdue = (
        t.due_date is not None
        and t.due_date < datetime.now(UTC).date()
        and t.status in (TaskStatus.OPEN, TaskStatus.IN_PROGRESS)
    )
    return {
        "ref": task_ref(t.id),
        "nazev": t.title,
        "popis": t.description,
        "projekt": project_name,
        "stav": t.status.value,
        "priorita": PRIORITY_LABEL.get(t.priority, str(t.priority)),
        "termin": t.due_date.isoformat() if t.due_date else None,
        "po_terminu": overdue,
        "zalozeno": t.created_at.date().isoformat(),
    }


async def _find_task(s: Any, ref: str, company_id: Any = None) -> Task:
    prefix = ref.strip().upper().removeprefix("T-").lower()
    if len(prefix) < 6 or not all(c in "0123456789abcdef" for c in prefix):
        raise ToolInputError(f"Neplatná reference úkolu '{ref}' (očekávám např. T-1a2b3c4d).")
    stmt = select(Task)
    if company_id is not None:
        stmt = stmt.where(Task.company_id == company_id)
    rows = (await s.scalars(stmt)).all()
    matches = [t for t in rows if str(t.id).replace("-", "").startswith(prefix)]
    if not matches:
        raise ToolInputError(f"Úkol {ref} neexistuje.")
    if len(matches) > 1:
        raise ToolInputError(f"Reference {ref} není jednoznačná.")
    found: Task = matches[0]
    return found


# ------------------------------------------------------------------ handlery


async def create_task(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    title = str(args.get("title") or "").strip()
    if not title:
        raise ToolInputError("Chybí název úkolu (title).")
    due = _parse_date(args.get("due_date"))
    priority = _parse_priority(args.get("priority"))
    async with ctx.sessions() as s, s.begin():
        project = await resolve_project(s, ctx, args.get("project"))
        task = Task(
            company_id=project.company_id,
            project_id=project.id,
            title=title[:300],
            description=(str(args["description"]).strip() if args.get("description") else None),
            due_date=due,
            priority=priority,
            source=f"{ctx.actor} via agent_run:{ctx.run_id}"[:200],
        )
        s.add(task)
        await s.flush()
        audit(
            s,
            actor=f"agent:{ctx.run_id}",
            action="task.created",
            entity_type="task",
            entity_id=task.id,
            company_id=project.company_id,
            requested_by=ctx.actor,
            values={
                "nazev": task.title,
                "termin": due.isoformat() if due else None,
                "priorita": PRIORITY_LABEL[priority],
                "stav": TaskStatus.OPEN.value,
            },
        )
        await s.refresh(task)
        return {"vytvoreno": True, **_task_dict(task, project.name)}


async def list_tasks(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    status_arg = str(args.get("status") or "open")
    async with ctx.sessions() as s:
        stmt = select(Task, Project.name).join(Project, Project.id == Task.project_id, isouter=True)
        project_name = None
        if ctx.company_id is not None:  # kanál projektu: jen úkoly téže firmy
            stmt = stmt.where(Task.company_id == ctx.company_id)
        if args.get("project") or (ctx.project_id and not args.get("all_projects")):
            project = await resolve_project(s, ctx, args.get("project"))
            stmt = stmt.where(Task.project_id == project.id)
            project_name = project.name
        if status_arg == "open":
            stmt = stmt.where(
                or_(Task.status == TaskStatus.OPEN, Task.status == TaskStatus.IN_PROGRESS)
            )
        elif status_arg != "all":
            if status_arg not in {x.value for x in TaskStatus}:
                raise ToolInputError("Stav: open, all, in_progress, done nebo cancelled.")
            stmt = stmt.where(Task.status == TaskStatus(status_arg))
        stmt = stmt.order_by(Task.due_date.asc().nulls_last(), Task.priority, Task.created_at)
        rows = (await s.execute(stmt.limit(MAX_LIST))).all()
    tasks = [_task_dict(t, pname) for t, pname in rows]
    return {
        "projekt": project_name or "všechny projekty",
        "filtr_stavu": status_arg,
        "pocet": len(tasks),
        "po_terminu": sum(1 for t in tasks if t["po_terminu"]),
        "ukoly": tasks,
    }


async def update_task(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ref = str(args.get("ref") or "")
    async with ctx.sessions() as s, s.begin():
        task = await _find_task(s, ref, ctx.company_id)
        changes: dict[str, Any] = {}
        if args.get("status"):
            new = str(args["status"])
            if new not in {x.value for x in TaskStatus}:
                raise ToolInputError("Stav: open, in_progress, done nebo cancelled.")
            changes["stav"] = [task.status.value, new]
            task.status = TaskStatus(new)
            task.completed_at = datetime.now(UTC) if new == TaskStatus.DONE.value else None
        if "due_date" in args:
            old_due = task.due_date.isoformat() if task.due_date else None
            task.due_date = _parse_date(args.get("due_date"))
            changes["termin"] = [old_due, task.due_date.isoformat() if task.due_date else None]
        if args.get("priority") not in (None, ""):
            old_p = PRIORITY_LABEL.get(task.priority, str(task.priority))
            task.priority = _parse_priority(args.get("priority"))
            changes["priorita"] = [old_p, PRIORITY_LABEL[task.priority]]
        if args.get("title"):
            old_title = task.title
            task.title = str(args["title"]).strip()[:300]
            changes["nazev"] = [old_title, task.title]
        if not changes:
            raise ToolInputError("Není co změnit (status, due_date, priority, title).")
        audit(
            s,
            actor=f"agent:{ctx.run_id}",
            action="task.updated",
            entity_type="task",
            entity_id=task.id,
            company_id=task.company_id,
            requested_by=ctx.actor,
            changes=changes,
        )
        project = await s.get(Project, task.project_id) if task.project_id else None
        await s.flush()
        await s.refresh(task)
        return {"zmeneno": changes, **_task_dict(task, project.name if project else None)}


async def task_history(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Historie úkolu z auditního logu (který nejde zpětně měnit)."""
    async with ctx.sessions() as s:
        task = await _find_task(s, str(args.get("ref") or ""), ctx.company_id)
        rows = (
            await s.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "task", AuditLog.entity_id == str(task.id))
                .order_by(AuditLog.id)
            )
        ).all()
    tz = ZoneInfo("Europe/Prague")
    return {
        "ref": task_ref(task.id),
        "nazev": task.title,
        "historie": [
            {
                "kdy": r.occurred_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                "akce": {"task.created": "založen", "task.updated": "změněn"}.get(
                    r.action, r.action
                ),
                "kdo": r.details.get("requested_by", r.actor),
                "hodnoty": r.details.get("values"),
                "zmeny": r.details.get("changes"),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------- definice

TASK_TOOLS = [
    Tool(
        name="task_history",
        description="Zobrazí historii úkolu: kdy a kým byl založen a jak se měnil stav, "
        "termín, priorita či název (změny jako [původní, nová]).",
        input_schema={
            "type": "object",
            "properties": {"ref": {"type": "string", "description": "Např. T-1a2b3c4d."}},
            "required": ["ref"],
        },
        level=PermissionLevel.AUTO,
        handler=task_history,
    ),
    Tool(
        name="create_task",
        description=(
            "Vytvoří interní úkol v evidenci (nic neodesílá ven). Použij, když uživatel řekne "
            "„přidej úkol“, „připomeň mi“, „musím…“ apod. Vrací ref T-xxxxxxxx."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Stručný název úkolu."},
                "description": {"type": "string"},
                "due_date": {"type": "string", "description": "Termín RRRR-MM-DD (volitelně)."},
                "priority": {
                    "type": "integer",
                    "enum": [1, 2, 3],
                    "description": "1 vysoká, 2 střední (výchozí), 3 nízká.",
                },
                "project": PROJECT_PARAM,
            },
            "required": ["title"],
        },
        level=PermissionLevel.AUTO,
        handler=create_task,
    ),
    Tool(
        name="list_tasks",
        description=(
            "Vypíše úkoly. Výchozí: otevřené úkoly projektu kanálu; v kanálu bez projektu "
            "úkoly všech projektů. Řazeno podle termínu; označuje úkoly po termínu."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["open", "all", "in_progress", "done", "cancelled"],
                },
                "project": PROJECT_PARAM,
                "all_projects": {"type": "boolean", "description": "Úkoly všech projektů."},
            },
        },
        level=PermissionLevel.AUTO,
        handler=list_tasks,
    ),
    Tool(
        name="update_task",
        description="Změní úkol podle ref (stav, termín, prioritu nebo název).",
        input_schema={
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Reference úkolu, např. T-1a2b3c4d."},
                "status": {"type": "string", "enum": ["open", "in_progress", "done", "cancelled"]},
                "due_date": {"type": ["string", "null"], "description": "RRRR-MM-DD nebo null."},
                "priority": {"type": "integer", "enum": [1, 2, 3]},
                "title": {"type": "string"},
            },
            "required": ["ref"],
        },
        level=PermissionLevel.AUTO,
        handler=update_task,
    ),
]
