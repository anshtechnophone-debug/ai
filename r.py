
import os
import time
import json
import asyncio
import logging
import secrets
import mimetypes
import socket
import platform
from pathlib import Path

import aiohttp


# ==================== CONFIG ====================

BOT_TOKEN = os.environ["BOT_TOKEN"]

OWNER_IDS = {8032922682, 8968928698}

BASE = Path.home() / "storage" / "shared"

PAGE_SIZE = 20
BATCH_SIZES = [10, 20, 50, 100, 500]
MAX_FILE_BYTES = 49 * 1024 * 1024
DIRECTORY_CACHE_SECONDS = 3
REQUEST_TIMEOUT = 180

API = f"https://api.telegram.org/bot{BOT_TOKEN}/"

logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# Callback token -> action
CALLBACKS = {}
DIRECTORY_CACHE = {}
TRANSFER_TASKS = {}
ACTIVE_TASKS = set()


# ==================== TELEGRAM API ====================

SESSION = None


async def api(method, data=None, upload=None):
    url = API + method
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

    try:
        if upload:
            form = aiohttp.FormData()

            for key, value in (data or {}).items():
                form.add_field(key, str(value))

            form.add_field(
                "document",
                upload["file"],
                filename=upload["filename"],
                content_type=upload["content_type"],
            )

            async with SESSION.post(
                url, data=form, timeout=timeout
            ) as response:
                result = await response.json(content_type=None)
        else:
            async with SESSION.post(
                url, json=data or {}, timeout=timeout
            ) as response:
                result = await response.json(content_type=None)

        if not result.get("ok"):
            logging.warning("%s failed: %s", method, result)

        return result

    except Exception:
        logging.exception("Telegram API error: %s", method)
        return {"ok": False, "description": "Request failed"}


async def send_message(chat_id, text, keyboard=None):
    data = {"chat_id": chat_id, "text": text}

    if keyboard:
        data["reply_markup"] = keyboard

    return await api("sendMessage", data)


async def edit_message(chat_id, message_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }

    if keyboard:
        data["reply_markup"] = keyboard

    return await api("editMessageText", data)


async def answer_callback(callback_id, text=None):
    data = {"callback_query_id": callback_id}

    if text:
        data["text"] = text

    await api("answerCallbackQuery", data)


# ==================== BUTTONS ====================

def button(label, action):
    token = secrets.token_urlsafe(7)
    CALLBACKS[token] = action

    return {
        "text": label,
        "callback_data": "x:" + token,
    }


def keyboard(rows):
    return {"inline_keyboard": rows}


def button_rows(items, width=2):
    return [
        items[i:i + width]
        for i in range(0, len(items), width)
    ]


def is_owner(user_id):
    return user_id in OWNER_IDS


# ==================== FILESYSTEM ====================

def safe_path(raw):
    path = Path(raw).resolve()
    root = BASE.resolve()

    if path != root and root not in path.parents:
        raise ValueError("Invalid path")

    return path


def device_name():
    name = ""

    try:
        import subprocess

        result = subprocess.run(
            ["getprop", "ro.product.model"],
            capture_output=True,
            text=True,
            timeout=2,
        )

        name = result.stdout.strip()

    except Exception:
        pass

    if not name:
        name = platform.node() or socket.gethostname()

    return name or "Android device"


def list_directory(folder, force=False):
    folder = safe_path(folder)
    key = str(folder)
    now = time.monotonic()

    if not force:
        cached = DIRECTORY_CACHE.get(key)

        if cached and now - cached[0] < DIRECTORY_CACHE_SECONDS:
            return cached[1]

    folders = []
    files = []

    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    path = Path(entry.path)

                    if entry.is_symlink():
                        continue

                    if entry.is_dir(follow_symlinks=False):
                        folders.append(path)

                    elif entry.is_file(follow_symlinks=False):
                        files.append(path)

                except (PermissionError, OSError):
                    continue

    except (PermissionError, OSError, FileNotFoundError):
        folders, files = [], []

    folders.sort(key=lambda p: p.name.lower())
    files.sort(key=lambda p: p.name.lower())

    result = (folders, files)
    DIRECTORY_CACHE[key] = (now, result)

    return result


def recursive_files(folder):
    """Only called when the user explicitly opens batch scanning."""
    folder = safe_path(folder)
    found = []

    for current, dirs, filenames in os.walk(
        folder, followlinks=False
    ):
        dirs[:] = sorted(
            [
                name for name in dirs
                if not (Path(current) / name).is_symlink()
            ],
            key=str.lower,
        )

        for filename in sorted(filenames, key=str.lower):
            path = Path(current) / filename

            try:
                if path.is_symlink() or not path.is_file():
                    continue

                found.append(path)

            except OSError:
                continue

    return found


def readable_size(size):
    if size < 1024:
        return f"{size} B"

    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"

    if size < 1024 ** 3:
        return f"{size / (1024 ** 2):.1f} MB"

    return f"{size / (1024 ** 3):.2f} GB"


# ==================== HOME / DEVICE ====================

def home_keyboard():
    return keyboard([
        [button("📱 " + device_name(), ("open", str(BASE), 0))],
    ])


async def show_home(chat_id, message_id=None):
    text = (
        "📂 Android File Explorer\n\n"
        "Tap your device to browse accessible shared storage."
    )

    if message_id:
        await edit_message(
            chat_id, message_id, text, home_keyboard()
        )
    else:
        await send_message(chat_id, text, home_keyboard())


# ==================== FOLDER SCREEN ====================

async def folder_screen(
    chat_id, message_id, folder, page=0, force=False
):
    try:
        folder = safe_path(folder)

    except (ValueError, OSError):
        await edit_message(
            chat_id, message_id, "Invalid folder path."
        )
        return

    if not folder.is_dir():
        await edit_message(
            chat_id, message_id, "Folder not found."
        )
        return

    # List only direct children; no recursive scan here.
    folders, files = await asyncio.to_thread(
        list_directory, folder, force
    )

    items = [("folder", p) for p in folders]
    items += [("file", p) for p in files]

    total = len(items)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))

    start = page * PAGE_SIZE
    visible = items[start:start + PAGE_SIZE]

    try:
        display_path = str(folder.relative_to(BASE.resolve()))

        if not display_path:
            display_path = "Shared storage"

    except ValueError:
        display_path = "Shared storage"

    lines = [
        f"📁 {display_path}",
        "",
        f"Folders here: {len(folders)}",
        f"Files here: {len(files)}",
        f"Items: {total}",
        f"Page: {page + 1}/{pages}",
        "",
    ]

    rows = []

    for kind, path in visible:
        if kind == "folder":
            rows.append([
                button("📁 " + path.name, ("open", str(path), 0))
            ])

        else:
            try:
                size = readable_size(path.stat().st_size)

            except OSError:
                size = "unknown size"

            rows.append([
                button(
                    f"📄 {path.name} ({size})",
                    ("send", str(path)),
                )
            ])

    navigation = []

    if page > 0:
        navigation.append(
            button(
                "⬅️ Previous",
                ("open", str(folder), page - 1),
            )
        )

    if page + 1 < pages:
        navigation.append(
            button(
                "Next ➡️",
                ("open", str(folder), page + 1),
            )
        )

    if navigation:
        rows.append(navigation)

    rows.append([
        button(
            "📦 Scan files / batches",
            ("batches", str(folder), 0),
        )
    ])

    if folder != BASE.resolve():
        rows.append([
            button(
                "⬆️ Parent folder",
                ("open", str(folder.parent), 0),
            )
        ])

    rows.append([
        button("🔄 Refresh", ("refresh", str(folder), page)),
        button("🏠 Home", ("home",)),
    ])

    await edit_message(
        chat_id,
        message_id,
        "\n".join(lines),
        keyboard(rows),
    )


# ==================== BATCH SCREEN ====================

async def batch_screen(chat_id, message_id, folder, page=0):
    try:
        folder = safe_path(folder)

    except (ValueError, OSError):
        await edit_message(
            chat_id, message_id, "Invalid folder path."
        )
        return

    await edit_message(
        chat_id,
        message_id,
        "🔎 Scanning accessible files...\n"
        "This may take a while for folders containing many files.",
    )

    try:
        files = await asyncio.to_thread(recursive_files, folder)

    except Exception:
        logging.exception("Recursive scan failed")

        await edit_message(
            chat_id, message_id, "Could not scan this folder."
        )
        return

    total = len(files)

    if total == 0:
        await edit_message(
            chat_id,
            message_id,
            "No accessible files found in this folder or its subfolders.",
            keyboard([
                [button("⬅️ Back", ("open", str(folder), 0))]
            ]),
        )
        return

    sizes = [size for size in BATCH_SIZES if size <= total]

    if not sizes:
        sizes = [total]

    rows = [
        [
            button(
                f"Send {start + 1}–{min(start + size, total)}",
                ("batch", str(folder), start, size),
            )
        ]
        for size in sizes
        for start in range(0, total, size)
    ]

    rows.append([
        button("⬅️ Back", ("open", str(folder), 0)),
        button("🏠 Home", ("home",)),
    ])

    await edit_message(
        chat_id,
        message_id,
        f"📦 Batch transfer\n\n"
        f"Files found: {total}\n"
        f"Folder: {folder.name or 'Shared storage'}\n\n"
        f"Choose a range to send. File order is alphabetical by folder.",
        keyboard(rows),
    )


# ==================== FILE TRANSFERS ====================

async def send_one(chat_id, path):
    try:
        path = safe_path(path)

        if not path.is_file():
            return False, f"File not found: {path.name}"

        size = path.stat().st_size

        if size > MAX_FILE_BYTES:
            return False, (
                f"Skipped {path.name}: {readable_size(size)} exceeds "
                f"the bot's configured upload limit."
            )

        content_type = (
            mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        )

        with path.open("rb") as file_handle:
            result = await api(
                "sendDocument",
                {"chat_id": chat_id},
                {
                    "file": file_handle,
                    "filename": path.name,
                    "content_type": content_type,
                },
            )

        if result.get("ok"):
            return True, path.name

        description = result.get(
            "description", "Telegram upload failed"
        )

        return False, f"{path.name}: {description}"

    except Exception as exc:
        logging.exception("File send failed")
        return False, f"{path.name}: {exc}"


async def transfer_range(
    chat_id, folder, start, size, message_id
):
    if chat_id in TRANSFER_TASKS:
        await edit_message(
            chat_id,
            message_id,
            "A transfer is already running in this chat. Please wait.",
        )
        return

    async def run_transfer():
        try:
            folder_path = safe_path(folder)

            files = await asyncio.to_thread(
                recursive_files, folder_path
            )

            total = len(files)
            selected = files[start:start + size]

            if not selected:
                await edit_message(
                    chat_id, message_id, "No files in that range."
                )
                return

            await edit_message(
                chat_id,
                message_id,
                f"📤 Sending files {start + 1}–"
                f"{start + len(selected)} of {total}...",
            )

            sent = 0
            failed = 0
            skipped = 0
            errors = []

            for index, path in enumerate(selected, start=1):
                ok, detail = await send_one(chat_id, path)

                if ok:
                    sent += 1

                else:
                    failed += 1

                    if (
                        "exceeds the bot's configured upload limit"
                        in detail
                    ):
                        skipped += 1

                    if len(errors) < 5:
                        errors.append(detail[:180])

                if index % 5 == 0 or index == len(selected):
                    status = (
                        f"📤 Transfer progress: "
                        f"{index}/{len(selected)}\n"
                        f"Sent: {sent} | Failed/skipped: {failed}"
                    )

                    await edit_message(
                        chat_id, message_id, status
                    )

            summary = (
                f"✅ Transfer finished\n\n"
                f"Selected: {len(selected)}\n"
                f"Sent: {sent}\n"
                f"Failed/skipped: {failed}"
            )

            if skipped:
                summary += f"\nToo large: {skipped}"

            if errors:
                summary += (
                    "\n\nSome errors:\n" + "\n".join(errors)
                )

            await edit_message(
                chat_id,
                message_id,
                summary,
                keyboard([
                    [
                        button(
                            "📁 Back to folder",
                            ("open", str(folder_path), 0),
                        ),
                        button("🏠 Home", ("home",)),
                    ]
                ]),
            )

        except Exception:
            logging.exception("Batch transfer failed")

            await edit_message(
                chat_id,
                message_id,
                "Transfer failed. Check bot.log.",
            )

    task = asyncio.create_task(run_transfer())
    TRANSFER_TASKS[chat_id] = task

    def clear_task(_):
        if TRANSFER_TASKS.get(chat_id) is task:
            TRANSFER_TASKS.pop(chat_id, None)

    task.add_done_callback(clear_task)


async def individual_transfer(chat_id, path, message_id):
    if chat_id in TRANSFER_TASKS:
        await edit_message(
            chat_id,
            message_id,
            "A transfer is already running in this chat. Please wait.",
        )
        return

    async def run_transfer():
        ok, detail = await send_one(chat_id, path)

        text = (
            f"✅ Sent: {detail}"
            if ok
            else f"❌ Could not send:\n{detail}"
        )

        try:
            await edit_message(
                chat_id,
                message_id,
                text,
                keyboard([
                    [
                        button(
                            "📁 Back",
                            ("open", str(Path(path).parent), 0),
                        ),
                        button("🏠 Home", ("home",)),
                    ]
                ]),
            )

        except Exception:
            logging.exception(
                "Could not update transfer status"
            )

    task = asyncio.create_task(run_transfer())
    TRANSFER_TASKS[chat_id] = task

    def clear_task(_):
        if TRANSFER_TASKS.get(chat_id) is task:
            TRANSFER_TASKS.pop(chat_id, None)

    task.add_done_callback(clear_task)


# ==================== UPDATE HANDLERS ====================

async def process_message(message):
    user = message.get("from", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    if not chat_id or not is_owner(user.get("id")):
        return

    if text.lower() in {"/start", "/device", "#device"}:
        await show_home(chat_id)
        return

    if text.lower() in {"#help", "/help"}:
        await send_message(
            chat_id,
            "Send #device to open the file explorer.\n"
            "Tap folders to browse and files to send them.\n"
            "Use Scan files / batches to choose file ranges.",
            home_keyboard(),
        )


async def process_callback(callback):
    user = callback.get("from", {})
    callback_id = callback.get("id")
    message = callback.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    data = callback.get("data", "")

    # Acknowledge promptly so Telegram's spinner stops.
    await answer_callback(callback_id)

    if not is_owner(user.get("id")):
        return

    if not chat_id or not message_id:
        return

    if not data.startswith("x:"):
        return

    action = CALLBACKS.get(data[2:])

    if not action:
        await edit_message(
            chat_id,
            message_id,
            "This button has expired. Send #device to reopen the explorer.",
            home_keyboard(),
        )
        return

    try:
        kind = action[0]

        if kind == "home":
            await show_home(chat_id, message_id)

        elif kind == "open":
            _, folder, page = action

            await folder_screen(
                chat_id, message_id, folder, page
            )

        elif kind == "refresh":
            _, folder, page = action

            DIRECTORY_CACHE.pop(
                str(safe_path(folder)), None
            )

            await folder_screen(
                chat_id,
                message_id,
                folder,
                page,
                force=True,
            )

        elif kind == "batches":
            _, folder, page = action

            await batch_screen(
                chat_id, message_id, folder, page
            )

        elif kind == "send":
            _, path = action

            await individual_transfer(
                chat_id, path, message_id
            )

        elif kind == "batch":
            _, folder, start, size = action

            await transfer_range(
                chat_id,
                folder,
                start,
                size,
                message_id,
            )

    except Exception:
        logging.exception("Callback processing error")

        await edit_message(
            chat_id,
            message_id,
            "Something went wrong. Check bot.log or send #device again.",
            home_keyboard(),
        )


async def handle_update(update):
    try:
        if "message" in update:
            await process_message(update["message"])

        elif "callback_query" in update:
            await process_callback(update["callback_query"])

    except Exception:
        logging.exception("Update handler error")


# ==================== MAIN POLLING LOOP ====================

async def main():
    global SESSION

    if not BASE.exists():
        raise RuntimeError(
            "Shared storage not found. Run termux-setup-storage first."
        )

    connector = aiohttp.TCPConnector(limit=20)
    SESSION = aiohttp.ClientSession(connector=connector)

    try:
        # Remove any old webhook so getUpdates can work.
        await api(
            "deleteWebhook",
            {"drop_pending_updates": False},
        )

        offset = None
        logging.info("File explorer bot started")

        # No print statements: keep Termux output silent.
        while True:
            params = {
                "timeout": 30,
                "allowed_updates": json.dumps(
                    ["message", "callback_query"]
                ),
            }

            if offset is not None:
                params["offset"] = offset

            try:
                async with SESSION.get(
                    API + "getUpdates",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=40),
                ) as response:
                    result = await response.json(
                        content_type=None
                    )

                if not result.get("ok"):
                    logging.warning(
                        "Polling error: %s", result
                    )
                    await asyncio.sleep(2)
                    continue

                for update in result.get("result", []):
                    offset = update["update_id"] + 1

                    task = asyncio.create_task(
                        handle_update(update)
                    )

                    ACTIVE_TASKS.add(task)
                    task.add_done_callback(
                        ACTIVE_TASKS.discard
                    )

            except asyncio.CancelledError:
                raise

            except Exception:
                logging.exception("Polling loop error")
                await asyncio.sleep(3)

    finally:
        await SESSION.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        pass
