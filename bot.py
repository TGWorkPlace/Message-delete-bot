import asyncio
import re
import os
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait, MessageDeleteForbidden, ChatAdminRequired

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

bot = Client("bulk_delete_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# State storage: user_id -> dict
user_states = {}

# ─── Helpers ────────────────────────────────────────────────────────────────

def parse_message_link(link: str):
    """Returns (chat_id_or_username, message_id) or None."""
    # https://t.me/username/123  or  https://t.me/c/1234567890/123
    m = re.match(r"https?://t\.me/c/(-?\d+)/(\d+)", link.strip())
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    m = re.match(r"https?://t\.me/([^/]+)/(\d+)", link.strip())
    if m:
        return m.group(1), int(m.group(2))
    return None

def type_buttons(selected: set) -> InlineKeyboardMarkup:
    types = [
        ("📄 Document", "document"),
        ("🖼 Image",    "photo"),
        ("🎬 Video",    "video"),
        ("🎵 Audio",    "audio"),
        ("🎭 Sticker",  "sticker"),
        ("💬 Text",     "text"),
    ]
    rows = []
    for label, key in types:
        tick = "✅ " if key in selected else ""
        rows.append([InlineKeyboardButton(f"{tick}{label}", callback_data=f"toggle_{key}")])
    rows.append([InlineKeyboardButton("☑️ All", callback_data="toggle_all")])
    rows.append([InlineKeyboardButton("🗑 Delete Now", callback_data="start_delete")])
    return InlineKeyboardMarkup(rows)

def get_msg_type(msg: Message) -> str:
    if msg.document:  return "document"
    if msg.photo:     return "photo"
    if msg.video:     return "video"
    if msg.audio:     return "audio"
    if msg.sticker:   return "sticker"
    if msg.text:      return "text"
    return "other"

# ─── /delete command ────────────────────────────────────────────────────────

@bot.on_message(filters.command("delete") & filters.private)
async def cmd_delete(client: Client, message: Message):
    user_states[message.from_user.id] = {"step": "await_first_link"}
    await message.reply(
        "📨 Send me the **first message link** from the channel.\n\n"
        "Format: `https://t.me/channelname/123`",
        quote=True,
    )

# ─── Conversation handler ────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.text & ~filters.command(["start", "delete", "help"]))
async def handle_links(client: Client, message: Message):
    uid = message.from_user.id
    state = user_states.get(uid)
    if not state:
        return

    step = state.get("step")

    if step == "await_first_link":
        parsed = parse_message_link(message.text)
        if not parsed:
            await message.reply("❌ Invalid link. Please send a valid Telegram message link.")
            return
        state["chat"], state["first_id"] = parsed
        state["step"] = "await_last_link"
        await message.reply(
            "✅ Got the first link!\n\n📨 Now send the **last message link** from the same channel.",
            quote=True,
        )

    elif step == "await_last_link":
        parsed = parse_message_link(message.text)
        if not parsed:
            await message.reply("❌ Invalid link. Please send a valid Telegram message link.")
            return
        chat2, last_id = parsed
        if chat2 != state["chat"]:
            await message.reply("❌ Both links must be from the same channel.")
            return
        if last_id < state["first_id"]:
            state["first_id"], last_id = last_id, state["first_id"]
        state["last_id"] = last_id
        state["step"] = "await_type_selection"
        state["selected_types"] = set()

        total = state["last_id"] - state["first_id"] + 1
        await message.reply(
            f"✅ Range set: messages **{state['first_id']}** → **{state['last_id']}** "
            f"({total} message IDs)\n\n"
            "🗂 Select the **message types** to delete, then press **Delete Now**:",
            reply_markup=type_buttons(set()),
            quote=True,
        )

# ─── Callback: toggle types & delete ─────────────────────────────────────────

@bot.on_callback_query()
async def handle_callback(client: Client, query: CallbackQuery):
    uid = query.from_user.id
    state = user_states.get(uid)
    data = query.data

    if not state or state.get("step") != "await_type_selection":
        await query.answer("Session expired. Use /delete to start again.", show_alert=True)
        return

    ALL_TYPES = {"document", "photo", "video", "audio", "sticker", "text"}
    selected: set = state.setdefault("selected_types", set())

    if data.startswith("toggle_"):
        key = data[len("toggle_"):]
        if key == "all":
            if selected == ALL_TYPES:
                selected.clear()
            else:
                selected.update(ALL_TYPES)
        else:
            if key in selected:
                selected.discard(key)
            else:
                selected.add(key)
        await query.edit_message_reply_markup(reply_markup=type_buttons(selected))
        await query.answer()

    elif data == "start_delete":
        if not selected:
            await query.answer("⚠️ Please select at least one message type.", show_alert=True)
            return

        await query.answer("🚀 Starting deletion…")
        await query.edit_message_text(
            f"⏳ Scanning and deleting messages…\nSelected types: {', '.join(sorted(selected))}"
        )

        chat = state["chat"]
        first_id = state["first_id"]
        last_id = state["last_id"]
        del user_states[uid]

        # ── Scan & delete ────────────────────────────────────────────────
        to_delete = []
        deleted_count = 0
        failed_count = 0
        scanned = 0
        total_range = last_id - first_id + 1

        status_msg = query.message

        BATCH = 200  # Telegram allows up to 200 ids per deleteMessages call

        async def flush_delete():
            nonlocal deleted_count, failed_count
            if not to_delete:
                return
            try:
                await client.delete_messages(chat, to_delete)
                deleted_count += len(to_delete)
            except (MessageDeleteForbidden, ChatAdminRequired) as e:
                failed_count += len(to_delete)
            except FloodWait as fw:
                await asyncio.sleep(fw.value + 1)
                try:
                    await client.delete_messages(chat, to_delete)
                    deleted_count += len(to_delete)
                except Exception:
                    failed_count += len(to_delete)
            except Exception:
                failed_count += len(to_delete)
            to_delete.clear()

        # Iterate in chunks of 200 IDs
        chunk_size = 200
        last_edit_time = 0

        for chunk_start in range(first_id, last_id + 1, chunk_size):
            chunk_end = min(chunk_start + chunk_size - 1, last_id)
            ids = list(range(chunk_start, chunk_end + 1))

            try:
                messages = await client.get_messages(chat, ids)
            except FloodWait as fw:
                await asyncio.sleep(fw.value + 1)
                try:
                    messages = await client.get_messages(chat, ids)
                except Exception:
                    scanned += len(ids)
                    continue
            except Exception:
                scanned += len(ids)
                continue

            if not isinstance(messages, list):
                messages = [messages]

            for msg in messages:
                scanned += 1
                if msg and not msg.empty:
                    msg_type = get_msg_type(msg)
                    if msg_type in selected:
                        to_delete.append(msg.id)

                if len(to_delete) >= BATCH:
                    await flush_delete()

            # Progress update every 5 seconds
            now = asyncio.get_event_loop().time()
            if now - last_edit_time >= 5:
                last_edit_time = now
                pct = int(scanned / total_range * 100)
                try:
                    await status_msg.edit_text(
                        f"⏳ Progress: {pct}%\n"
                        f"Scanned: {scanned}/{total_range}\n"
                        f"Queued to delete: {len(to_delete)}\n"
                        f"Deleted so far: {deleted_count}"
                    )
                except Exception:
                    pass

        # Flush remaining
        await flush_delete()

        await status_msg.edit_text(
            "✅ **Deletion Complete!**\n\n"
            f"📊 **Summary**\n"
            f"• Total IDs scanned: `{scanned}`\n"
            f"• Messages deleted: `{deleted_count}`\n"
            f"• Failed/skipped: `{failed_count}`\n"
            f"• Types deleted: `{', '.join(sorted(selected))}`"
        )

# ─── /start & /help ──────────────────────────────────────────────────────────

@bot.on_message(filters.command(["start", "help"]) & filters.private)
async def cmd_start(client: Client, message: Message):
    await message.reply(
        "👋 **Bulk Message Delete Bot**\n\n"
        "Use `/delete` to start a bulk deletion session.\n\n"
        "**Steps:**\n"
        "1. Send the first message link\n"
        "2. Send the last message link\n"
        "3. Choose message types (Document, Image, Video, Audio, Sticker, Text, or All)\n"
        "4. Press **Delete Now**\n\n"
        "⚠️ The bot must be an **admin with Delete Messages** permission in the channel.",
        quote=True,
    )

# ─── Health check for Koyeb ─────────────────────────────────────────────────

async def health(request):
    return web.Response(text="OK")

async def run_web():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    print("Health server running on port 8080")

# ─── Entry point ─────────────────────────────────────────────────────────────

async def main():
    await run_web()
    await bot.start()
    print("Bot started!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
