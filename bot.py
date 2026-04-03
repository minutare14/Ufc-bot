import asyncio
import logging
import os
from datetime import datetime, timedelta

import aiohttp
from bs4 import BeautifulSoup
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

UFC_BASE = "https://www.ufc.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def fetch_html(url: str) -> str | None:
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.text()
    except Exception as exc:
        print(f"Erro ao buscar {url}: {exc}")
    return None


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


def parse_events(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    events = []

    cards = soup.select("article.c-card-event--result")

    for card in cards:
        title_el = card.select_one(".c-card-event--result__headline")
        date_el = card.select_one("time")
        loc_el = card.select_one(".c-card-event--result__location")
        link_el = card.select_one("a[href]")

        title = title_el.get_text(strip=True) if title_el else "Evento UFC"
        date_str = date_el.get("datetime", "") if date_el else ""
        location = loc_el.get_text(strip=True) if loc_el else "A confirmar"
        link = UFC_BASE + link_el["href"] if link_el else ""

        event_date = None
        try:
            event_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except Exception:
            pass

        events.append(
            {
                "title": title,
                "date": event_date,
                "date_str": date_str,
                "location": location,
                "link": link,
            }
        )

    return events


def filter_weekend_events(events: list[dict]) -> list[dict]:
    today = datetime.now()
    days_until_saturday = (5 - today.weekday()) % 7 or 7
    next_saturday = today + timedelta(days=days_until_saturday)
    next_sunday = next_saturday + timedelta(days=1)

    weekend = [
        e
        for e in events
        if e.get("date")
        and next_saturday.date() <= e["date"].date() <= next_sunday.date()
    ]

    if not weekend:
        weekend = [
            e for e in events if e.get("date") and e["date"] > today
        ][:3]

    return weekend


async def fetch_event_fights(event_url: str) -> list[dict]:
    html = await fetch_html(event_url)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    fights = []

    for row in soup.select(".c-listing-fight"):
        red_el = row.select_one(".c-listing-fight__corner-body--red")
        blue_el = row.select_one(".c-listing-fight__corner-body--blue")
        weight_el = row.select_one(".c-listing-fight__class-text")

        red = red_el.get_text(" ", strip=True)[:40] if red_el else "?"
        blue = blue_el.get_text(" ", strip=True)[:40] if blue_el else "?"
        weight = weight_el.get_text(strip=True) if weight_el else ""
        title_fight = bool(row.select_one(".c-listing-fight__championship"))

        fights.append(
            {
                "red": red,
                "blue": blue,
                "weight": weight,
                "title_fight": title_fight,
            }
        )

    return fights


async def search_fighter_in_events(
    name: str, events: list[dict]
) -> list[dict]:
    name_lower = name.lower()
    found = []

    for ev in events[:8]:
        if not ev.get("link"):
            continue
        fights = await fetch_event_fights(ev["link"])
        await asyncio.sleep(0.5)
        for fight in fights:
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

    html = await fetch_html(f"{UFC_BASE}/events")
    if not html:
        await msg.edit_text("Nao consegui acessar ufc.com.")
        return

    events = parse_events(html)
    weekend = filter_weekend_events(events)

    if not weekend:
        await msg.edit_text("Nenhum evento no proximo fim de semana.")
        return

    text = "*Eventos UFC — Proximo Fim de Semana*\n\n"
    for ev in weekend:
        date = (
            ev["date"].strftime("%d/%m/%Y %H:%M")
            if ev.get("date")
            else ev["date_str"]
        )
        text += (
            f"*{ev['title']}*\n"
            f"Data: {date}\n"
            f"Local: {ev['location']}\n"
            f"[Ver card]({ev['link']})\n\n"
        )

    await msg.edit_text(
        text, parse_mode="Markdown", disable_web_page_preview=True
    )


async def cmd_eventos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.effective_message.reply_text("Buscando eventos...")

    html = await fetch_html(f"{UFC_BASE}/events")
    if not html:
        await msg.edit_text("Nao consegui acessar ufc.com.")
        return

    events = parse_events(html)
    now = datetime.now()
    upcoming = [e for e in events if not e.get("date") or e["date"] >= now][
        :6
    ]

    if not upcoming:
        await msg.edit_text("Nenhum evento futuro encontrado.")
        return

    text = "*Proximos Eventos UFC*\n\n"
    for ev in upcoming:
        date = (
            ev["date"].strftime("%d/%m/%Y")
            if ev.get("date")
            else ev["date_str"]
        )
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

    html = await fetch_html(f"{UFC_BASE}/events")
    if not html:
        await msg.edit_text("Erro ao acessar ufc.com.")
        return

    events = parse_events(html)
    now = datetime.now()
    future = [e for e in events if not e.get("date") or e["date"] >= now]
    found_fights = await search_fighter_in_events(name, future)

    if not found_fights:
        await msg.edit_text(
            f"*{name}* nao encontrado em proximos eventos.\n\nVeja todos com /eventos",
            parse_mode="Markdown",
        )
        return

    text = f"*{name}* — Proximas lutas:\n\n"
    for item in found_fights:
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
            webhook_url=f"{WEBHOOK_URL}/webhook",
            secret_token=SECRET_TOKEN or None,
            drop_pending_updates=True,
        )
    else:
        logger.warning("WEBHOOK_URL nao definida — usando polling (fallback).")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
