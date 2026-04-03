import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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

UFC_ICS_URL = "https://raw.githubusercontent.com/clarencechaan/ufc-cal/ics/UFC.ics"
SP_TZ = ZoneInfo("America/Sao_Paulo")

# Headers padrão para busca do ICS
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/plain,text/html,*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
}

# Headers de Googlebot — sites whitelistam para indexação SEO
GOOGLEBOT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

DAYS_PT = {
    0: "Segunda-feira",
    1: "Terça-feira",
    2: "Quarta-feira",
    3: "Quinta-feira",
    4: "Sexta-feira",
    5: "Sábado",
    6: "Domingo",
}

DIVISIONS = {
    "115": "Palha",
    "125": "Mosca",
    "135": "Galo",
    "145": "Pena",
    "155": "Leve",
    "170": "Meio-Médio",
    "185": "Médio",
    "205": "Meio-Pesado",
    "265": "Pesado",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def fetch_text(url: str, timeout: int = 15, headers: dict | None = None) -> str | None:
    hdrs = headers or HEADERS
    try:
        async with aiohttp.ClientSession(headers=hdrs) as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                logger.info("GET %s → %s", url, resp.status)
                if resp.status == 200:
                    return await resp.text()
                logger.warning("Status %s para %s", resp.status, url)
    except Exception as exc:
        logger.error("Erro ao buscar %s: %s", url, exc)
    return None


def _extract_og_image(html: str) -> str | None:
    for pattern in (
        r'property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    ):
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


async def _reddit_poster(event_title: str) -> str | None:
    """Busca poster no r/ufc via API pública do Reddit.
    A comunidade sempre posta o poster oficial antes de cada evento."""
    # "UFC Fight Night: Moicano vs Duncan" → "Moicano Duncan poster"
    names = re.sub(r"UFC\s*(Fight Night|[0-9]+)\s*:?\s*", "", event_title, flags=re.I).strip()
    query = re.sub(r"\s+vs\.?\s+", " ", names, flags=re.I).strip() + " poster"
    encoded = query.replace(" ", "+")
    url = (
        f"https://www.reddit.com/r/ufc/search.json"
        f"?q={encoded}&sort=new&limit=15&restrict_sr=1&type=link"
    )
    reddit_hdrs = {
        "User-Agent": "ufc-telegram-bot/1.0 (Telegram event tracker)",
        "Accept": "application/json",
    }
    text = await fetch_text(url, timeout=10, headers=reddit_hdrs)
    if not text:
        return None
    try:
        data = json.loads(text)
        for post in data.get("data", {}).get("children", []):
            pd = post.get("data", {})
            # Post de imagem direta (i.redd.it, imgur, etc.)
            post_url = pd.get("url", "")
            if re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", post_url, re.I):
                logger.info("Reddit poster: %s", post_url)
                return post_url
            # Preview gerado pelo Reddit
            for img in pd.get("preview", {}).get("images", [])[:1]:
                src = img.get("source", {}).get("url", "")
                if src:
                    return src.replace("&amp;", "&")
    except Exception as exc:
        logger.warning("Reddit parse error: %s", exc)
    return None


async def _bing_image_search(query: str) -> str | None:
    """Busca imagem no Bing Images (scraping, sem API key)."""
    encoded = re.sub(r"[^a-zA-Z0-9 ]", "", query).strip().replace(" ", "+")
    url = f"https://www.bing.com/images/search?q={encoded}&first=1&count=3"
    bing_hdrs = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.bing.com/",
    }
    html = await fetch_text(url, timeout=8, headers=bing_hdrs)
    if not html:
        return None
    # Bing embute dados de imagem em JSON: {"iurl":"...","murl":"..."}
    for field in ("iurl", "murl"):
        m = re.search(rf'"{field}"\s*:\s*"(https?://[^"]+)"', html)
        if m:
            logger.info("Bing %s: %s", field, m.group(1)[:100])
            return m.group(1)
    return None


async def _ufc_og_image(event_url: str) -> str | None:
    """Tenta og:image do ufc.com com Googlebot e depois UA normal."""
    for hdrs in (GOOGLEBOT_HEADERS, HEADERS):
        html = await fetch_text(event_url, timeout=6, headers=hdrs)
        if html:
            img = _extract_og_image(html)
            if img:
                return img
    return None


async def fetch_event_image(event_url: str, event_title: str = "") -> str | None:
    """Busca imagem em três fontes em paralelo — retorna a primeira válida."""
    sources = []
    if event_title:
        sources.append(_reddit_poster(event_title))       # 1ª: Reddit r/ufc
        sources.append(_bing_image_search(f"{event_title} fight poster"))  # 2ª: Bing
    if event_url and event_url.startswith("http"):
        sources.append(_ufc_og_image(event_url))          # 3ª: UFC.com

    for coro in asyncio.as_completed(sources):
        try:
            result = await coro
            if result:
                logger.info("Imagem obtida: %s", result[:100])
                return result
        except Exception as exc:
            logger.warning("Falha na busca de imagem: %s", exc)

    logger.warning("Nenhuma imagem encontrada para '%s'", event_title)
    return None


# ---------------------------------------------------------------------------
# Formatação
# ---------------------------------------------------------------------------


def fmt_sp(dt: datetime) -> str:
    """Converte UTC → São Paulo e retorna string legível em pt-BR."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_sp = dt.astimezone(SP_TZ)
    day_name = DAYS_PT[dt_sp.weekday()]
    return dt_sp.strftime(f"{day_name}, %d/%m/%Y às %H:%M (Brasília)")


def weight_label(w: str) -> str:
    return DIVISIONS.get(w.strip(), f"@{w}" if w else "")


# ---------------------------------------------------------------------------
# iCalendar parser (stdlib puro)
# ---------------------------------------------------------------------------


def _parse_ics_datetime(value: str) -> datetime | None:
    value = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _unescape_ics(value: str) -> str:
    return value.replace("\\,", ",").replace("\\n", "\n").replace("\\;", ";")


def _parse_fights_from_description(desc: str) -> list[dict]:
    fights = []
    section = "main"
    for line in desc.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if "prelim" in lower and "vs." not in lower:
            section = "prelim"
            continue
        if "main card" in lower and "vs." not in lower:
            section = "main"
            continue
        fight_line = stripped.lstrip("•").strip()
        if " vs. " not in fight_line:
            continue
        parts = fight_line.split(" vs. ", 1)
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
        fights.append(
            {
                "red": red[:70],
                "blue": blue[:70],
                "weight": weight,
                "title_fight": "(C)" in red or "(C)" in blue,
                "section": section,
            }
        )
    return fights


def parse_ics(content: str) -> list[dict]:
    events = []
    current: dict | None = None

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
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
            key = key.split(";")[0].strip()
            current[key] = value.strip()
            current["__last_key"] = key

    result = []
    for ev in events:
        title = _unescape_ics(ev.get("SUMMARY", "Evento UFC"))
        event_date = _parse_ics_datetime(ev.get("DTSTART", ""))
        location = _unescape_ics(ev.get("LOCATION", "A confirmar"))
        description = _unescape_ics(ev.get("DESCRIPTION", ""))
        link = _unescape_ics(ev.get("UID", ""))
        fights = _parse_fights_from_description(description)
        result.append(
            {
                "title": title,
                "date": event_date,
                "date_str": event_date.strftime("%d/%m/%Y") if event_date else "",
                "location": location,
                "link": link,
                "fights": fights,
            }
        )

    result.sort(
        key=lambda e: e["date"] or datetime.max.replace(tzinfo=timezone.utc)
    )
    logger.info("ICS: %d eventos parseados", len(result))
    return result


# ---------------------------------------------------------------------------
# Busca e filtro
# ---------------------------------------------------------------------------


async def get_events() -> list[dict]:
    content = await fetch_text(UFC_ICS_URL)
    if content:
        events = parse_ics(content)
        if events:
            return events
    logger.error("Nao foi possivel obter o calendario ICS.")
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
# Montagem do card
# ---------------------------------------------------------------------------


def build_card_text(ev: dict) -> str:
    date_str = fmt_sp(ev["date"]) if ev.get("date") else ev.get("date_str", "?")
    fights = ev.get("fights", [])
    main = [f for f in fights if f["section"] == "main"]
    prelims = [f for f in fights if f["section"] == "prelim"]

    text = f"*{ev['title']}*\n"
    text += f"📅 {date_str}\n"
    text += f"📍 {ev['location']}\n"

    if main:
        text += "\n*— Main Card —*\n"
        for i, f in enumerate(main):
            div = weight_label(f["weight"])
            belt = " 🏆" if f["title_fight"] else ""
            div_str = f"  _{div}_" if div else ""
            if i == 0:
                # Luta principal em destaque
                text += f"\n🥊 *{f['red']}*\n     vs\n🥊 *{f['blue']}*{belt}{div_str}\n\n"
            else:
                text += f"• {f['red']} vs {f['blue']}{belt}{div_str}\n"

    if prelims:
        text += "\n*— Prelims —*\n"
        for f in prelims:
            div = weight_label(f["weight"])
            div_str = f"  _{div}_" if div else ""
            text += f"• {f['red']} vs {f['blue']}{div_str}\n"

    text += f"\n[Ver card completo]({ev['link']})"
    return text


# ---------------------------------------------------------------------------
# Envio do card com imagem
# ---------------------------------------------------------------------------


async def _send_event_card(update: Update, ev: dict) -> None:
    card_text = build_card_text(ev)
    image_url = await fetch_event_image(ev["link"], ev.get("title", ""))

    if image_url:
        try:
            if len(card_text) <= 1024:
                await update.effective_message.reply_photo(
                    photo=image_url,
                    caption=card_text,
                    parse_mode="Markdown",
                )
            else:
                # Foto com título + data; card completo como mensagem seguinte
                header = (
                    f"*{ev['title']}*\n"
                    f"📅 {fmt_sp(ev['date']) if ev.get('date') else ev.get('date_str','')}\n"
                    f"📍 {ev['location']}"
                )
                await update.effective_message.reply_photo(
                    photo=image_url,
                    caption=header,
                    parse_mode="Markdown",
                )
                await update.effective_message.reply_text(
                    card_text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            return
        except Exception as exc:
            logger.warning("Falha ao enviar foto: %s", exc)

    # Fallback: só texto
    await update.effective_message.reply_text(
        card_text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


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
        "`/fds` — Card do próximo fim de semana\n"
        "`/eventos` — Próximos eventos\n"
        "`/lutador [nome]` — Quando um lutador vai lutar",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def cmd_fds(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.effective_message.reply_text("Buscando card do fim de semana...")

    events = await get_events()
    if not events:
        await msg.edit_text("Não consegui obter eventos. Tente novamente.")
        return

    weekend = filter_weekend_events(events)
    if not weekend:
        await msg.edit_text("Nenhum evento no próximo fim de semana.")
        return

    await msg.delete()
    for ev in weekend:
        await _send_event_card(update, ev)


async def cmd_eventos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.effective_message.reply_text("Buscando eventos...")

    events = await get_events()
    if not events:
        await msg.edit_text("Não consegui obter eventos. Tente novamente.")
        return

    now_utc = datetime.now(timezone.utc)
    upcoming = [e for e in events if e.get("date") and e["date"] >= now_utc][:6]
    if not upcoming:
        upcoming = events[:6]

    text = "*Próximos Eventos UFC*\n\n"
    for ev in upcoming:
        date_str = fmt_sp(ev["date"]) if ev.get("date") else ev.get("date_str", "?")
        text += (
            f"*{ev['title']}*\n"
            f"📅 {date_str}\n"
            f"📍 {ev['location']}\n"
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
            f"*{name}* não encontrado em próximos eventos.\n\nVeja todos com /eventos",
            parse_mode="Markdown",
        )
        return

    await msg.delete()
    for item in found:
        ev = item["event"]
        fight = item["fight"]
        date_str = fmt_sp(ev["date"]) if ev.get("date") else "?"
        div = weight_label(fight["weight"])
        belt = " 🏆" if fight["title_fight"] else ""
        div_str = f"\n_{div}_" if div else ""

        text = (
            f"*{ev['title']}*\n"
            f"📅 {date_str}\n"
            f"📍 {ev['location']}\n\n"
            f"🥊 *{fight['red']}*\n     vs\n🥊 *{fight['blue']}*{belt}{div_str}\n\n"
            f"[Ver evento]({ev['link']})"
        )
        image_url = await fetch_event_image(ev["link"], ev.get("title", ""))
        if image_url:
            try:
                await update.effective_message.reply_photo(
                    photo=image_url, caption=text, parse_mode="Markdown"
                )
                continue
            except Exception as exc:
                logger.warning("Falha foto: %s", exc)
        await update.effective_message.reply_text(
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
        raise RuntimeError("TELEGRAM_TOKEN não definido.")

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
        logger.warning("WEBHOOK_URL não definida — usando polling (fallback).")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
