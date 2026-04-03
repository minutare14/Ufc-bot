import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

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
UFCSTATS_BASE = "https://ufcstats.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
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
                logger.info("GET %s → %s", url, resp.status)
                if resp.status == 200:
                    return await resp.text()
                logger.warning("Status inesperado %s para %s", resp.status, url)
    except Exception as exc:
        logger.error("Erro ao buscar %s: %s", url, exc)
    return None


# ---------------------------------------------------------------------------
# Scraper — fonte primária: ufcstats.com
# ---------------------------------------------------------------------------


def _parse_date(raw: str) -> datetime | None:
    """Tenta parsear datas nos formatos usados pelo ufcstats e ufc.com."""
    raw = raw.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # ISO com offset (ufc.com)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def parse_events_ufcstats(html: str) -> list[dict]:
    """Parseia eventos de ufcstats.com/statistics/events/upcoming."""
    soup = BeautifulSoup(html, "lxml")
    events = []

    rows = soup.select("tr.b-statistics__table-row")
    logger.info("ufcstats: %d linhas encontradas", len(rows))

    for row in rows:
        cols = row.select("td.b-statistics__table-col")
        if len(cols) < 3:
            continue

        link_el = cols[0].select_one("a")
        if not link_el:
            continue

        title = link_el.get_text(strip=True)
        link = link_el.get("href", "")
        date_raw = cols[1].get_text(strip=True)
        location = cols[2].get_text(strip=True)
        event_date = _parse_date(date_raw)

        events.append(
            {
                "title": title,
                "date": event_date,
                "date_str": date_raw,
                "location": location,
                "link": link,
            }
        )

    logger.info("ufcstats: %d eventos parseados", len(events))
    return events


def parse_events_ufc_jsonld(html: str) -> list[dict]:
    """Extrai eventos do JSON-LD embutido nas páginas do ufc.com."""
    soup = BeautifulSoup(html, "lxml")
    events = []

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") not in ("SportsEvent", "Event"):
                    continue
                title = item.get("name", "Evento UFC")
                date_raw = item.get("startDate", "")
                location_obj = item.get("location", {})
                location = (
                    location_obj.get("name", "A confirmar")
                    if isinstance(location_obj, dict)
                    else str(location_obj)
                )
                url = item.get("url", "")
                event_date = _parse_date(date_raw)
                events.append(
                    {
                        "title": title,
                        "date": event_date,
                        "date_str": date_raw,
                        "location": location,
                        "link": url,
                    }
                )
        except Exception:
            pass

    # fallback: seletores CSS legados do ufc.com
    if not events:
        cards = soup.select("article.c-card-event--result")
        logger.info("ufc.com CSS fallback: %d cards encontrados", len(cards))
        for card in cards:
            title_el = card.select_one(".c-card-event--result__headline")
            date_el = card.select_one("time")
            loc_el = card.select_one(".c-card-event--result__location")
            link_el = card.select_one("a[href]")

            title = title_el.get_text(strip=True) if title_el else "Evento UFC"
            date_str = date_el.get("datetime", "") if date_el else ""
            location = loc_el.get_text(strip=True) if loc_el else "A confirmar"
            link = UFC_BASE + link_el["href"] if link_el else ""
            event_date = _parse_date(date_str)

            events.append(
                {
                    "title": title,
                    "date": event_date,
                    "date_str": date_str,
                    "location": location,
                    "link": link,
                }
            )

    logger.info("ufc.com: %d eventos extraídos (JSON-LD + CSS)", len(events))
    return events


async def get_events() -> list[dict]:
    """Busca eventos: tenta ufcstats primeiro, depois ufc.com."""
    html = await fetch_html(f"{UFCSTATS_BASE}/statistics/events/upcoming")
    if html:
        events = parse_events_ufcstats(html)
        if events:
            return events

    logger.warning("ufcstats falhou ou vazio — tentando ufc.com")
    html = await fetch_html(f"{UFC_BASE}/events")
    if html:
        return parse_events_ufc_jsonld(html)

    return []


def filter_weekend_events(events: list[dict]) -> list[dict]:
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    days_until_saturday = (5 - today.weekday()) % 7 or 7
    next_saturday = today + timedelta(days=days_until_saturday)
    next_sunday = next_saturday + timedelta(days=1)

    logger.info(
        "Filtrando fim de semana: %s a %s (hoje=%s)",
        next_saturday, next_sunday, today,
    )

    def event_date(e: dict):
        d = e.get("date")
        if d is None:
            return None
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d

    weekend = [
        e for e in events
        if event_date(e) and next_saturday <= event_date(e).date() <= next_sunday
    ]

    if not weekend:
        logger.info("Nenhum evento no FDS — usando fallback (próximos 3)")
        weekend = [
            e for e in events
            if event_date(e) and event_date(e) >= now_utc
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
        date = (
            ev["date"].strftime("%d/%m/%Y %H:%M UTC")
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

    events = await get_events()
    if not events:
        await msg.edit_text("Nao consegui obter eventos. Tente novamente.")
        return

    now_utc = datetime.now(timezone.utc)

    def is_future(e: dict) -> bool:
        d = e.get("date")
        if d is None:
            return True
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return d >= now_utc

    upcoming = [e for e in events if is_future(e)][:6]
    if not upcoming:
        upcoming = events[:6]

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

    events = await get_events()
    if not events:
        await msg.edit_text("Erro ao buscar eventos.")
        return

    found_fights = await search_fighter_in_events(name, events)

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
