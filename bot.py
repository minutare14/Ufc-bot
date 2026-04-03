import logging
import os
from datetime import datetime, timedelta, timezone

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
SECRET_TOKEN = os.getenv("SECRET_TOKEN", "")
PORT = int(os.getenv("PORT", "8765"))

UFC_ICS_URL = (
    "https://raw.githubusercontent.com/clarencechaan/ufc-cal/ics/UFC.ics"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/plain,text/html,*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
}


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


async def fetch_text(url: str) -> str | None:
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                logger.info("GET %s → %s", url, resp.status)
                if resp.status == 200:
                    return await resp.text()
                logger.warning("Status %s para %s", resp.status, url)
    except Exception as exc:
        logger.error("Erro ao buscar %s: %s", url, exc)
    return None


# ---------------------------------------------------------------------------
# iCalendar parser (sem dependência externa)
# ---------------------------------------------------------------------------


def _parse_ics_datetime(value: str) -> datetime | None:
    """Parseia DTSTART do formato iCal: 20260405T000000Z ou 20260405."""
    value = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _unescape_ics(value: str) -> str:
    return value.replace("\\,", ",").replace("\\n", "\n").replace("\\;", ";")


def parse_ics(content: str) -> list[dict]:
    """Parseia o arquivo .ics e retorna lista de eventos ordenados por data."""
    events = []
    current: dict | None = None

    for raw_line in content.splitlines():
        line = raw_line.rstrip()

        # Linha de continuação (começa com espaço ou tab)
        if current is not None and raw_line and raw_line[0] in (" ", "\t"):
            last_key = current.get("__last_key")
            if last_key:
                current[last_key] = current[last_key] + line.strip()
            continue

        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, _, value = line.partition(":")
            # Remove parâmetros (ex: DTSTART;TZID=America/New_York → DTSTART)
            key = key.split(";")[0].strip()
            current[key] = value.strip()
            current["__last_key"] = key

    result = []
    for ev in events:
        title = _unescape_ics(ev.get("SUMMARY", "Evento UFC"))
        dtstart_raw = ev.get("DTSTART", "")
        event_date = _parse_ics_datetime(dtstart_raw)
        location = _unescape_ics(ev.get("LOCATION", "A confirmar"))
        description = _unescape_ics(ev.get("DESCRIPTION", ""))
        link = _unescape_ics(ev.get("UID", ""))

        fights = _parse_fights_from_description(description)

        result.append(
            {
                "title": title,
                "date": event_date,
                "date_str": event_date.strftime("%d/%m/%Y") if event_date else dtstart_raw,
                "location": location,
                "link": link,
                "description": description,
                "fights": fights,
            }
        )

    result.sort(key=lambda e: e["date"] or datetime.max.replace(tzinfo=timezone.utc))
    logger.info("ICS: %d eventos parseados", len(result))
    return result


def _parse_fights_from_description(desc: str) -> list[dict]:
    """Extrai lutas da DESCRIPTION do .ics (formato: '• Lutador A vs. Lutador B @peso')."""
    fights = []
    for line in desc.splitlines():
        line = line.strip().lstrip("•").strip()
        if " vs. " not in line:
            continue
        parts = line.split(" vs. ", 1)
        if len(parts) != 2:
            continue
        red = parts[0].strip()
        blue_weight = parts[1].strip()
        weight = ""
        if "@" in blue_weight:
            blue, _, weight = blue_weight.rpartition("@")
            blue = blue.strip()
            weight = weight.strip()
        else:
            blue = blue_weight
        title_fight = "(C)" in red or "(C)" in blue
        fights.append(
            {
                "red": red[:60],
                "blue": blue[:60],
                "weight": weight,
                "title_fight": title_fight,
            }
        )
    return fights


# ---------------------------------------------------------------------------
# Busca de eventos e filtro de fim de semana
# ---------------------------------------------------------------------------


async def get_events() -> list[dict]:
    content = await fetch_text(UFC_ICS_URL)
    if content:
        events = parse_ics(content)
        if events:
            return events
    logger.error("Nao foi possivel obter eventos do calendario ICS.")
    return []


def filter_weekend_events(events: list[dict]) -> list[dict]:
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    days_until_saturday = (5 - today.weekday()) % 7 or 7
    next_saturday = today + timedelta(days=days_until_saturday)
    next_sunday = next_saturday + timedelta(days=1)

    logger.info("FDS buscado: %s a %s", next_saturday, next_sunday)

    def norm(e: dict) -> datetime | None:
        d = e.get("date")
        if d is None:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    weekend = [
        e for e in events
        if norm(e) and next_saturday <= norm(e).date() <= next_sunday
    ]

    if not weekend:
        logger.info("Sem evento no FDS — mostrando próximo(s)")
        weekend = [e for e in events if norm(e) and norm(e) >= now_utc][:3]

    return weekend


def search_fighter(name: str, events: list[dict]) -> list[dict]:
    """Busca lutador nos cards já carregados (sem HTTP extra)."""
    name_lower = name.lower()
    found = []
    for ev in events:
        for fight in ev.get("fights", []):
            if (
                name_lower in fight["red"].lower()
                or name_lower in fight["blue"].lower()
            ):
                found.append({"event": ev, "fight": fight})
    return found


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("Fim de semana", callback_data="weekend")],
        [InlineKeyboardButton("Todos os eventos", callback_data="all_events")],
        [InlineKeyboardButton("Buscar lutador", callback_data="search_fighter")],
    ]
    await update.message.reply_text(
        "*UFC Bot*\n\n"
        "`/fds` — Eventos do próximo fim de semana\n"
        "`/eventos` — Próximos eventos\n"
        "`/lutador [nome]` — Quando um lutador vai lutar",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def cmd_fds(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.effective_message.reply_text(
        "Buscando eventos do fim de semana..."
    )

    events = await get_events()
    if not events:
        await msg.edit_text("Nao consegui obter eventos. Tente novamente.")
        return

    weekend = filter_weekend_events(events)
    if not weekend:
        await msg.edit_text("Nenhum evento no proximo fim de semana.")
        return

    text = "*Eventos UFC — Proximo Fim de Semana*\n\n"
    for ev in weekend:
        date = ev["date"].strftime("%d/%m/%Y %H:%M UTC") if ev.get("date") else ev["date_str"]
        fights_preview = ""
        main_fights = [f for f in ev.get("fights", [])[:3]]
        for f in main_fights:
            belt = " C" if f["title_fight"] else ""
            fights_preview += f"  • {f['red']} vs {f['blue']}{belt}\n"

        text += (
            f"*{ev['title']}*\n"
            f"Data: {date}\n"
            f"Local: {ev['location']}\n"
            + (f"{fights_preview}" if fights_preview else "")
            + f"[Ver card]({ev['link']})\n\n"
        )

    await msg.edit_text(
        text, parse_mode="Markdown", disable_web_page_preview=True
    )


async def cmd_eventos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.effective_message.reply_text("Buscando eventos...")

    events = await get_events()
    if not events:
        await msg.edit_text("Nao consegui obter eventos. Tente novamente.")
        return

    now_utc = datetime.now(timezone.utc)
    upcoming = [
        e for e in events
        if e.get("date") and e["date"] >= now_utc
    ][:6]
    if not upcoming:
        upcoming = events[:6]

    text = "*Proximos Eventos UFC*\n\n"
    for ev in upcoming:
        date = ev["date"].strftime("%d/%m/%Y") if ev.get("date") else ev["date_str"]
        text += (
            f"*{ev['title']}*\n"
            f"Data: {date} | Local: {ev['location']}\n"
            f"[Ver card]({ev['link']})\n\n"
        )

    await msg.edit_text(
        text, parse_mode="Markdown", disable_web_page_preview=True
    )


async def cmd_lutador(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Use: `/lutador Nome do Lutador`\nExemplo: `/lutador Jon Jones`",
            parse_mode="Markdown",
        )
        return

    name = " ".join(ctx.args)
    msg = await update.message.reply_text(
        f"Buscando *{name}*...", parse_mode="Markdown"
    )

    events = await get_events()
    if not events:
        await msg.edit_text("Erro ao buscar eventos.")
        return

    found = search_fighter(name, events)
    if not found:
        await msg.edit_text(
            f"*{name}* nao encontrado em proximos eventos.\n\nVeja todos com /eventos",
            parse_mode="Markdown",
        )
        return

    text = f"*{name}* — Proximas lutas:\n\n"
    for item in found:
        ev = item["event"]
        fight = item["fight"]
        date = ev["date"].strftime("%d/%m/%Y") if ev.get("date") else "?"
        belt = " _(cinturao)_" if fight["title_fight"] else ""
        text += (
            f"*{ev['title']}*\n"
            f"Data: {date} | Local: {ev['location']}\n"
            f"*{fight['red']}* vs *{fight['blue']}*{belt}\n"
            f"[Ver evento]({ev['link']})\n\n"
        )

    await msg.edit_text(
        text, parse_mode="Markdown", disable_web_page_preview=True
    )


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "weekend":
        ctx.args = []
        await cmd_fds(update, ctx)
    elif query.data == "all_events":
        await cmd_eventos(update, ctx)
    elif query.data == "search_fighter":
        await query.edit_message_text(
            "Digite o nome do lutador no chat ou use:\n`/lutador Nome`",
            parse_mode="Markdown",
        )


async def msg_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.args = update.message.text.strip().split()
    await cmd_lutador(update, ctx)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN nao definido.")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("fds", cmd_fds))
    app.add_handler(CommandHandler("eventos", cmd_eventos))
    app.add_handler(CommandHandler("lutador", cmd_lutador))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler)
    )

    if WEBHOOK_URL:
        logger.info("Iniciando em modo WEBHOOK: %s", WEBHOOK_URL)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="webhook",
            webhook_url=f"{WEBHOOK_URL}/webhook",
            secret_token=SECRET_TOKEN or None,
            drop_pending_updates=True,
        )
    else:
        logger.warning("WEBHOOK_URL nao definida — usando polling (fallback).")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
