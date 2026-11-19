#!/usr/bin/env python3
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

ROOT = Path("public/data/schedules")
STATE_PATH = Path(os.environ.get("TELEGRAM_STATE_PATH", str(ROOT / "telegram-state.json")))
CHANNEL_ID = os.environ.get("TELEGRAM_CHAT_ID", "@PoltavaLight")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SITE_URL = "https://svitlo-ua.vercel.app/poltava"
KYIV = ZoneInfo("Europe/Kyiv")
WIDTH = 1400
ROW_HEIGHT = 42
LEFT = 145
RIGHT = 70
CHART_WIDTH = WIDTH - LEFT - RIGHT
COLORS = {
    "on": (105, 183, 137),
    "off": (221, 104, 101),
    "switching": (235, 185, 83),
}

def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu") / name,
        Path("/usr/share/fonts/dejavu") / name,
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()

def load_state():
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"entries": {}}
    except (OSError, json.JSONDecodeError):
        return {"entries": {}}

def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

def telegram_request(url, data, headers, label, timeout=35):
    """Надійне надсилання до Telegram із повторними спробами при мережевих збоях."""
    attempts = 4
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                raise RuntimeError(f"{label}: Telegram повернув помилку: {result}")
            return result
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt == attempts:
                raise RuntimeError(f"{label}: HTTP {error.code}: {details}") from error
            retry_after = None
            try:
                retry_after = json.loads(details).get("parameters", {}).get("retry_after")
            except (json.JSONDecodeError, AttributeError):
                pass
            delay = min(60, int(retry_after)) if retry_after else 2 ** (attempt + 1)
            print(f"{label}: HTTP {error.code}; повтор {attempt + 1}/{attempts} через {delay} с")
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as error:
            if attempt == attempts:
                raise RuntimeError(f"{label}: не вдалося після {attempts} спроб: {error}") from error
            delay = 2 ** (attempt + 1)
            print(f"{label}: мережева помилка ({error}); повтор {attempt + 1}/{attempts} через {delay} с")
            time.sleep(delay)
    raise RuntimeError(f"{label}: вичерпано всі спроби надсилання")


def load_schedule(date_key):
    path = ROOT / f"{date_key}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    queues = data.get("queues")
    expected = [f"{q}.{sub}" for q in range(1, 7) for sub in (1, 2)]
    if not isinstance(queues, dict) or set(queues) != set(expected):
        raise ValueError(f"{date_key}: очікуються всі 12 підчерг")
    for queue in expected:
        if not isinstance(queues[queue], list) or len(queues[queue]) != 48:
            raise ValueError(f"{date_key}: підчерга {queue} не містить 48 півгодинних інтервалів")
        if any(value not in COLORS for value in queues[queue]):
            raise ValueError(f"{date_key}: невідомий стан у підчерзі {queue}")
    return {"date": date_key, "queues": queues, "fetchedAt": data.get("fetchedAt", "")}

def make_image(schedule, kind, previous=None):
    import math

    queues = schedule["queues"]
    width, height = 960, 1280
    scale = 2
    image = Image.new("RGB", (width * scale, height * scale), (255, 255, 255))
    draw = ImageDraw.Draw(image)

    palette = {
        "on": (146, 209, 79),
        "off": (255, 182, 193),
        "switching": (235, 230, 100),
    }
    ink = (17, 20, 22)
    muted = (95, 98, 100)
    date_obj = datetime.strptime(schedule["date"], "%Y-%m-%d").replace(tzinfo=KYIV)
    weekday_names = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")
    date_label = f"{weekday_names[date_obj.weekday()]} {date_obj:%d.%m.%Y}"

    def point(x, y):
        return (int(round(x * scale)), int(round(y * scale)))

    def scaled_font(size, bold=False):
        return font(size * scale, bold)

    def sector_points(cx, cy, inner_r, outer_r, start_angle, end_angle):
        points = []
        steps = max(4, int((end_angle - start_angle) * 2))
        for step in range(steps + 1):
            angle = math.radians(start_angle + (end_angle - start_angle) * step / steps)
            points.append(point(
                cx + outer_r * math.cos(angle),
                cy + outer_r * math.sin(angle),
            ))
        for step in range(steps, -1, -1):
            angle = math.radians(start_angle + (end_angle - start_angle) * step / steps)
            points.append(point(
                cx + inner_r * math.cos(angle),
                cy + inner_r * math.sin(angle),
            ))
        return points

    def draw_changed_hatch(polygon):
        left = max(0, min(p[0] for p in polygon) - 2 * scale)
        top = max(0, min(p[1] for p in polygon) - 2 * scale)
        right = min(width * scale, max(p[0] for p in polygon) + 2 * scale)
        bottom = min(height * scale, max(p[1] for p in polygon) + 2 * scale)
        patch_w, patch_h = max(1, right - left), max(1, bottom - top)
        local_polygon = [(x - left, y - top) for x, y in polygon]

        mask = Image.new("L", (patch_w, patch_h), 0)
        ImageDraw.Draw(mask).polygon(local_polygon, fill=255)

        hatch = Image.new("RGBA", (patch_w, patch_h), (0, 0, 0, 0))
        hatch_draw = ImageDraw.Draw(hatch)
        spacing = 5 * scale
        stripe_width = max(1, scale)
        for offset in range(-patch_h, patch_w, spacing):
            hatch_draw.line(
                (offset, 0, offset + patch_h, patch_h),
                fill=(*palette["off"], 245),
                width=stripe_width,
            )
            hatch_draw.line(
                (offset + spacing // 2, 0, offset + spacing // 2 + patch_h, patch_h),
                fill=(*palette["switching"], 245),
                width=stripe_width,
            )
        image.paste(hatch, (left, top), mask)

    centers_x = (164, 480, 796)
    centers_y = (145, 455, 765, 1075)
    center_radius = 70

    for index in range(12):
        queue = f"{index // 2 + 1}.{index % 2 + 1}"
        cx, cy = centers_x[index % 3], centers_y[index // 3]
        values = queues[queue]
        old_values = previous.get(queue, []) if previous else []
        has_previous = len(old_values) == 48

        # Each 30-minute state occupies 15 degrees. The hour labels sit on
        # the boundary between the two half-hour slices, exactly like the reference.
        rings = ((70, 100, 0), (103, 137, 24))
        for inner_r, outer_r, slot_offset in rings:
            for slot in range(24):
                slot_index = slot_offset + slot
                start_angle = -90 + slot * 15
                end_angle = start_angle + 15
                polygon = sector_points(
                    cx, cy, inner_r, outer_r, start_angle, end_angle
                )
                # No white gaps between adjacent half-hour sectors.
                draw.polygon(polygon, fill=palette[values[slot_index]])

                if has_previous and old_values[slot_index] != values[slot_index]:
                    draw_changed_hatch(polygon)

        # The last half-hour (23:30–24:00) slightly extends beyond the outer ring.
        # This small terminal cap is present in the supplied reference chart.
        final_cap = sector_points(cx, cy, 137, 142, -105, -90)
        draw.polygon(final_cap, fill=palette[values[47]])
        if has_previous and old_values[47] != values[47]:
            draw_changed_hatch(final_cap)

        # Thin hour marks cross the colored rings. The text labels cover the
        # middle of each mark, leaving the short tick marks visible around them.
        for hour in range(12):
            angle = math.radians(-90 + hour * 30)
            for inner_r, outer_r in ((70, 100), (103, 137)):
                draw.line(
                    (
                        point(cx + inner_r * math.cos(angle), cy + inner_r * math.sin(angle)),
                        point(cx + outer_r * math.cos(angle), cy + outer_r * math.sin(angle)),
                    ),
                    fill=(54, 59, 61),
                    width=scale,
                )

        # Short half-hour ticks on the ring edges, alternating with longer hour ticks.
        for tick in range(24):
            angle = math.radians(-90 + tick * 15)
            is_hour = tick % 2 == 0
            inner_end = 105 if is_hour else 103
            outer_end = 143 if is_hour else 140
            draw.line(
                (
                    point(cx + 100 * math.cos(angle), cy + 100 * math.sin(angle)),
                    point(cx + inner_end * math.cos(angle), cy + inner_end * math.sin(angle)),
                ),
                fill=(93, 98, 100),
                width=scale,
            )
            draw.line(
                (
                    point(cx + 137 * math.cos(angle), cy + 137 * math.sin(angle)),
                    point(cx + outer_end * math.cos(angle), cy + outer_end * math.sin(angle)),
                ),
                fill=(93, 98, 100),
                width=scale,
            )

        # Fine circular outlines separate the white center, inner ring and outer ring.
        for radius, color in ((70, (255, 255, 255)), (100, (242, 242, 242)), (103, (242, 242, 242))):
            draw.ellipse(
                (
                    point(cx - radius, cy - radius),
                    point(cx + radius, cy + radius),
                ),
                outline=color,
                width=scale,
            )

        # Hour numbers lie on radial positions, not in the middle of the sectors.
        for hour in range(12):
            angle = math.radians(-90 + hour * 30)
            draw.text(
                point(cx + 78 * math.cos(angle), cy + 78 * math.sin(angle)),
                str(hour),
                font=scaled_font(17, True),
                fill=ink,
                anchor="mm",
            )
            draw.text(
                point(cx + 121 * math.cos(angle), cy + 121 * math.sin(angle)),
                str(hour + 12),
                font=scaled_font(17, True),
                fill=ink,
                anchor="mm",
            )

        # White center with queue name, explanation, date and hour totals.
        draw.ellipse(
            (
                point(cx - center_radius, cy - center_radius),
                point(cx + center_radius, cy + center_radius),
            ),
            fill=(255, 255, 255),
        )
        draw.text(point(cx, cy - 21), f"{queue} черга",
                  font=scaled_font(19, True), fill=ink, anchor="mm")
        draw.text(point(cx, cy + 2), "при запланованому",
                  font=scaled_font(12), fill=ink, anchor="mm")
        draw.text(point(cx, cy + 17), "графіку відключень",
                  font=scaled_font(12), fill=ink, anchor="mm")
        draw.text(point(cx, cy + 33), date_label,
                  font=scaled_font(12, True), fill=ink, anchor="mm")

        off_hours = values.count("off") / 2
        switching_hours = values.count("switching") / 2
        on_hours = values.count("on") / 2

        def number_text(value):
            text_value = f"{value:g}"
            return text_value.replace(".", ",")

        summary = [
            (f"-{number_text(off_hours)}", (194, 143, 157), False),
            (number_text(switching_hours), ink, True),
            (f"+{number_text(on_hours)}", (112, 163, 78), False),
        ]
        summary_font = scaled_font(15, True)
        widths = [
            draw.textbbox((0, 0), label, font=summary_font)[2]
            for label, _, _ in summary
        ]
        gap = 3 * scale
        total_width = sum(widths) + 2 * gap
        cursor_x = cx * scale - total_width / 2
        baseline_y = (cy + 54) * scale
        for (label, color, highlighted), text_width in zip(summary, widths):
            if highlighted:
                draw.rounded_rectangle(
                    (
                        int(cursor_x - 2 * scale),
                        int(baseline_y - 10 * scale),
                        int(cursor_x + text_width + 2 * scale),
                        int(baseline_y + 8 * scale),
                    ),
                    radius=2 * scale,
                    fill=(255, 232, 73),
                )
            draw.text(
                (int(cursor_x), int(baseline_y)),
                label,
                font=summary_font,
                fill=color,
                anchor="ls",
            )
            cursor_x += text_width + gap

    # Bottom legend follows the same order and compact horizontal arrangement.
    legend_y = 1252
    legend_items = (
        ("світло є", palette["on"], 31, 145, False),
        ("світла немає", palette["off"], 180, 185, False),
        ("перемикання (до 30 хв)", palette["switching"], 370, 294, False),
        ("зміни (штриховка)", palette["on"], 669, 250, True),
    )
    for label, color, x, item_width, hatched in legend_items:
        draw.rounded_rectangle(
            (
                point(x, legend_y - 11),
                point(x + 22, legend_y + 11),
            ),
            radius=2 * scale,
            fill=color,
        )
        if hatched:
            patch_w = patch_h = 22 * scale
            stripe_mask = Image.new("L", (patch_w, patch_h), 0)
            ImageDraw.Draw(stripe_mask).rounded_rectangle(
                (0, 0, patch_w - 1, patch_h - 1), radius=2 * scale, fill=255
            )
            stripe_layer = Image.new("RGBA", (patch_w, patch_h), (0, 0, 0, 0))
            stripe_draw = ImageDraw.Draw(stripe_layer)
            for offset in range(-patch_h, patch_w, 6 * scale):
                stripe_draw.line(
                    (offset, 0, offset + patch_h, patch_h),
                    fill=(*palette["off"], 255),
                    width=2 * scale,
                )
                stripe_draw.line(
                    (offset + 3 * scale, 0, offset + 3 * scale + patch_h, patch_h),
                    fill=(*palette["switching"], 255),
                    width=2 * scale,
                )
            image.paste(
                stripe_layer,
                point(x, legend_y - 11),
                stripe_mask,
            )
            draw = ImageDraw.Draw(image)
        draw.text(
            point(x + 30, legend_y + 1),
            label,
            font=scaled_font(15),
            fill=(52, 54, 56),
            anchor="lm",
        )

    # Tiny generation time in the lower-right corner, as in the reference.
    generated_at = datetime.now(KYIV).strftime("%H:%M") + " · №2"
    draw.text(
        point(938, 1222),
        generated_at,
        font=scaled_font(8),
        fill=(145, 145, 145),
        anchor="ra",
    )

    return image.resize((width, height), Image.Resampling.LANCZOS)

def duration_text(slots):
    minutes = slots * 30
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} год {mins} хв"
    if hours:
        return f"{hours} год"
    return f"{mins} хв"

def compare_queues(previous, current):
    increased, decreased, shifted, possible = [], [], [], []
    for queue in sorted(current):
        old = previous.get(queue, [])
        new = current[queue]
        if len(old) != 48:
            continue
        old_off = old.count("off")
        new_off = new.count("off")
        delta = new_off - old_off
        if delta > 0:
            increased.append(f"{queue} (+{duration_text(delta)})")
        elif delta < 0:
            decreased.append(f"{queue} (−{duration_text(-delta)})")
        elif old != new and old_off == new_off:
            shifted.append(queue)
    return increased, decreased, shifted

def build_caption(date_key, kind, previous, current):
    date_obj = datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=KYIV)
    date_label = date_obj.strftime("%d.%m.%Y")
    period = "на завтра" if kind == "tomorrow" else "на сьогодні"

    if previous is None:
        return (
            f"🆕 Опубліковано графік погодинних відключень {period} — {date_label}.\n\n"
            "Перегляньте актуальний графік для своєї черги на нашому сайті."
        )

    increased, decreased, shifted = compare_queues(previous, current)
    lines = [f"🔄 Оновлено графік погодинних відключень {period} — {date_label}."]
    if decreased:
        queues = ", ".join(item.split(" (", 1)[0] for item in decreased)
        lines.append(f"🟢 Для черг {queues} — зменшили відключення.")
    if increased:
        queues = ", ".join(item.split(" (", 1)[0] for item in increased)
        lines.append(f"🔴 Для черг {queues} — збільшили відключення.")
    if shifted:
        lines.append("🕒 Для черг " + ", ".join(shifted) + " — змінилися години відключень.")
    if len(lines) == 1:
        lines.append("Час відключень для черг не змінився.")
    lines.extend(["", "Актуальний графік для всіх черг доступний на нашому сайті."])
    return "\n".join(lines)

def initialize_status_timing(state):
    """Підхоплює історичне запізнення зі збережених півгодинних повідомлень."""
    timing = state.setdefault("statusTiming", {})
    if timing.get("historyImported"):
        return

    historical_delays = []
    for item in state.get("statusMessageIds", []):
        sent_at = item.get("sentAt", "")
        try:
            sent = datetime.fromisoformat(sent_at)
        except (TypeError, ValueError):
            continue

        delay_seconds = (sent.minute % 30) * 60 + sent.second
        if 0 <= delay_seconds <= 10 * 60:
            historical_delays.append(delay_seconds)

    timing["delaySamples"] = historical_delays[-12:]
    timing["historyImported"] = True
    if historical_delays:
        average_late = sum(historical_delays) / len(historical_delays)
        timing["averageLateSeconds"] = round(average_late)
        timing["leadMinutes"] = min(
            12, max(5, int((average_late + 59) // 60) + 2)
        )
    else:
        timing.setdefault("leadMinutes", 5)

    save_state(state)
    print(
        f"Історія затримок: {len(historical_delays)} повідомлень, "
        f"середнє запізнення {timing.get('averageLateSeconds', 0)} с, "
        f"випередження {timing.get('leadMinutes', 5)} хв"
    )


def status_message_order(message_key):
    """Повертає числовий порядок дати й півгодинного слота для lastKey."""
    if not isinstance(message_key, str) or ":" not in message_key:
        return None
    try:
        date_key, slot_text = message_key.rsplit(":", 1)
        date_value = datetime.strptime(date_key, "%Y-%m-%d").date()
        slot = int(slot_text)
        if not 0 <= slot < 48:
            return None
        return date_value.toordinal() * 48 + slot
    except (TypeError, ValueError):
        return None


def select_status_target(now, last_key):
    """Обирає наступний півгодинний інтервал після контрольної межі публікації."""
    from datetime import timedelta

    boundary = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
    target_start = boundary + timedelta(minutes=30)
    target_key = target_start.date().isoformat()
    slot = target_start.hour * 2 + (1 if target_start.minute >= 30 else 0)
    message_key = f"{target_key}:{slot}"
    candidate_order = status_message_order(message_key)
    last_order = status_message_order(last_key)

    # О 21:00 публікуємо прогноз на 21:30–22:00, а не на 21:00–21:30.
    # Запізнілий запуск усе одно надсилає інтервал після запланованої межі.
    if last_order is not None and last_order >= candidate_order:
        return None

    return target_key, slot, message_key, target_start

def forecast_boundary_for_dispatch(now, advance_seconds):
    """Maps a delayed Vercel dispatch to its intended :00/:30 publication boundary."""
    from datetime import timedelta

    reference_now = now + timedelta(seconds=advance_seconds)
    return reference_now.replace(
        minute=(reference_now.minute // 30) * 30,
        second=0,
        microsecond=0,
    )


def send_status_message(schedule, state, now):
    """Публікує прогноз для першого ще не опублікованого півгодинного інтервалу."""
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задано")

    boundary_advance = int(os.environ.get("TELEGRAM_BOUNDARY_ADVANCE_SECONDS", "0"))
    boundary = forecast_boundary_for_dispatch(now, boundary_advance)
    wait_seconds = (boundary - now).total_seconds()
    if boundary_advance > 0 and wait_seconds > 0:
        print(f"Очікування контрольної межі: {wait_seconds:.1f} с")
        time.sleep(wait_seconds)
        # Після очікування використовуємо фактичний час надсилання, а не час старту workflow.
        now = datetime.now(KYIV)

    status_state = state.setdefault("statusMessages", {})
    target = select_status_target(boundary, status_state.get("lastKey"))
    if target is None:
        print(
            f"Прогноз пропущено: кандидат для {now:%H:%M:%S} уже опублікований "
            f"або запуск застарів; останній слот {status_state.get('lastKey')}."
        )
        return

    target_key, slot, message_key, forecast_start = target
    target_schedule = schedule if schedule["date"] == target_key else load_schedule(target_key)
    if not target_schedule:
        print(f"Немає графіка для прогнозу на {target_key}")
        return

    time_label = f"{slot // 2:02d}:{'30' if slot % 2 else '00'}"

    queues = target_schedule["queues"]
    previous_slot = slot - 1
    turning_off, turning_on, without_power = [], [], []

    for queue in sorted(queues, key=lambda value: tuple(map(int, value.split(".")))):
        current = queues[queue][slot]
        previous = queues[queue][previous_slot] if previous_slot >= 0 else None

        # Перемикання відносимо до тих, хто вмикається.
        if current == "switching":
            turning_on.append(queue)
        elif current == "off":
            if previous == "on":
                turning_off.append(queue)
            else:
                without_power.append(queue)
        elif current == "on" and previous in ("off", "switching"):
            turning_on.append(queue)

    no_changes = not turning_off and not turning_on and not without_power

    date_label = datetime.strptime(target_key, "%Y-%m-%d").strftime("%d.%m.%Y")
    end_slot = (slot + 1) % 48
    end_time = f"{end_slot // 2:02d}:{'30' if end_slot % 2 else '00'}"

    first_outage_alert = status_state.get("firstOutageAlertDate") != target_key
    lines = [
        f"💡 <b>Прогноз відключень на {time_label}</b>",
        "",
        f"Інформація для Полтавської області на інтервал {time_label}–{end_time}.",
    ]
    if turning_off:
        lines.extend(["", f"🔴 <b>Вимкнення:</b> {', '.join(turning_off)}"])
    if not (first_outage_alert and turning_off):
        if turning_on:
            lines.extend(["", f"🟢 <b>Увімкнення:</b> {', '.join(turning_on)}"])
        if without_power:
            lines.extend(["", f"⚫ <b>Без світла:</b> {', '.join(without_power)}"])
    if no_changes:
        lines.extend(["", "🟢 <b>Планових вимкнень і перемикань за графіком немає.</b>"])
    lines.extend(["", f"📅 <b>{date_label}</b>", "",
                  'Детальний графік для вашої черги — <a href="https://svitlo-ua.vercel.app/poltava"><b>Світло.ua · Полтавщина</b></a>'])
    text = "\n".join(lines)

    payload = json.dumps({
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                {
                    "text": "⚡ Переглянути графіки",
                    "url": "https://svitlo-ua.vercel.app/poltava",
                }
            ]]
        },
    }).encode("utf-8")
    result = telegram_request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        payload,
        {"Content-Type": "application/json"},
        "Telegram-прогноз",
        timeout=35,
    )

    # Зберігаємо лише ID повідомлень півгодинних прогнозів.
    # Фото графіків та повідомлення про оновлення сюди ніколи не потрапляють.
    status_messages = state.setdefault("statusMessageIds", [])
    status_messages.append({
        "message_id": result["result"]["message_id"],
        "sentAt": now.isoformat(),
    })
    state["statusMessageIds"] = status_messages[-300:]

    status_state["lastKey"] = message_key
    status_state["sentAt"] = now.isoformat()
    if first_outage_alert and turning_off:
        status_state["firstOutageAlertDate"] = target_key
        print(f"Перший прогноз із вимкненнями за {target_key}: короткий формат")

    save_state(state)
    boundary = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
    delay_seconds = int((now - boundary).total_seconds())
    if delay_seconds >= 0:
        timing_note = f"через {delay_seconds} с після контрольної межі"
    else:
        timing_note = f"за {-delay_seconds} с до контрольної межі"
    print(
        f"Telegram: прогноз на {target_key} {time_label} успішно надіслано; "
        f"{timing_note}."
    )



def cleanup_status_messages(state, now):
    """Під час кожного запуску прибирає прогнози, старші за дві години."""
    from datetime import timedelta

    cleanup_state = state.setdefault("statusCleanup", {})
    cutoff = now - timedelta(hours=2)

    messages = state.setdefault("statusMessageIds", [])
    remaining = []
    deleted = 0

    for item in messages:
        message_id = item.get("message_id")
        try:
            sent_at = datetime.fromisoformat(item.get("sentAt", ""))
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=KYIV)
        except (TypeError, ValueError):
            # Без достовірного часу не видаляємо повідомлення навмання.
            remaining.append(item)
            print(f"Не видаляю прогноз {message_id}: невідомий час публікації")
            continue

        if sent_at > cutoff:
            remaining.append(item)
            continue

        try:
            payload = json.dumps({
                "chat_id": CHANNEL_ID,
                "message_id": message_id,
            }).encode("utf-8")
            telegram_request(
                f"https://api.telegram.org/bot{TOKEN}/deleteMessage",
                payload,
                {"Content-Type": "application/json"},
                f"Видалення півгодинного прогнозу {message_id}",
                timeout=20,
            )
            deleted += 1
        except Exception as error:
            # Залишаємо запис, якщо Telegram не підтвердив видалення.
            remaining.append(item)
            print(f"Не вдалося видалити прогноз {message_id}: {error}", file=sys.stderr)

    state["statusMessageIds"] = remaining
    cleanup_state["lastRunAt"] = now.isoformat()
    save_state(state)
    print(f"Очищення прогнозів: видалено {deleted} повідомлень старше двох годин; залишено {len(remaining)}")


def send_test_messages(schedule, state, now):
    """Одноразова перевірка півгодинного повідомлення та надсилання графіка."""
    if os.environ.get("TELEGRAM_SKIP_TESTS") == "1":
        return
    if not Path("scripts/telegram-test-pending.txt").exists():
        return
    test_id = Path("scripts/telegram-test-pending.txt").read_text(encoding="utf-8").strip()
    test_state = state.setdefault("telegramTests", {})
    if test_state.get("lastTestId") != test_id:
        test_state["lastTestId"] = test_id
        test_state["forecastSent"] = False
        test_state["scheduleSent"] = False
        save_state(state)

    if not test_state.get("forecastSent"):
        test_text = (
            "🧪 <b>ТЕСТ: прогноз відключень на 19:30</b>\n\n"
            "Це тестове повідомлення для перевірки формату півгодинного прогнозу.\n\n"
            "🔴 <b>Вимкнення:</b> 1.1, 2.2\n"
            "🟢 <b>Увімкнення:</b> 3.1, 4.2\n"
            "⚫ <b>Без світла:</b> 5.1, 6.2\n\n"
            "📅 <b>Тестове повідомлення</b>"
        )
        payload = json.dumps({
            "chat_id": CHANNEL_ID,
            "text": test_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {
                "inline_keyboard": [[{
                    "text": "⚡ Переглянути графіки",
                    "url": "https://svitlo-ua.vercel.app/poltava",
                }]]
            },
        }).encode("utf-8")
        telegram_request(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            payload,
            {"Content-Type": "application/json"},
            "Тест півгодинного прогнозу",
            timeout=35,
        )
        test_state["forecastSent"] = True
        save_state(state)

    if not test_state.get("scheduleSent"):
        send_schedule_update(
            "🧪 ТЕСТ: повідомлення про оновлення графіка. Нижче є кнопка переходу на сайт."
        )
        test_state["scheduleSent"] = True
        save_state(state)
        print("Telegram: тестові повідомлення надіслано")



def send_photo(image, caption):
    import io
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    boundary = "----SvitloTelegramBoundary"
    fields = {
        "chat_id": CHANNEL_ID,
        "caption": caption[:1024],
    }
    body = bytearray()
    for key, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(b'Content-Disposition: form-data; name="photo"; filename="poltava-schedule.png"\r\n')
    body.extend(b"Content-Type: image/png\r\n\r\n")
    body.extend(buffer.getvalue())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    telegram_request(
        f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
        bytes(body),
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        "Telegram-графік",
        timeout=60,
    )
    print("Telegram: фото успішно надіслано")

def build_hourly_message(schedule, now):
    hour = now.hour
    slot = hour * 2
    queues = schedule["queues"]
    switching_off = []
    switching_on = []
    without_power = []
    possible = []

    for queue in sorted(queues):
        values = queues[queue]
        current = values[slot]
        previous = values[slot - 1] if slot > 0 else None

        if current == "switching":
            possible.append(queue)
        elif current == "off":
            if previous == "on":
                switching_off.append(queue)
            else:
                without_power.append(queue)
        elif current == "on" and previous == "off":
            switching_on.append(queue)

    def names(items):
        return ", ".join(items) if items else "—"

    date_label = now.strftime("%d.%m.%Y")
    return (
        f"💡 <b>Світло на Полтавщині · {hour:02d}:00</b>\n\n"
        f"Станом на {hour:02d}:00 на Полтавщині ситуація з електропостачанням буде такою "
        f"({date_label}):\n\n"
        f"🔴 <b>Вимикаються черги:</b> {names(switching_off)}\n\n"
        f"🟢 <b>Вмикаються черги:</b> {names(switching_on)}\n\n"
        f"⚫ <b>Без світла черги:</b> {names(without_power)}"
        + (f"\n\n🟡 <b>Можливе перемикання:</b> {names(possible)}" if possible else "")
        + "\n\nПерегляньте інформацію за своєю чергою на "
        + '<a href="https://energy-ua.info/">Energy-UA</a>.'
        + "\n\n<a href=\"https://t.me/+Wk2HBi5w2UJmYThi\">Energy-UA⚡️Полтава</a>"
    )

def send_schedule_update(message):
    """Надсилає повідомлення про зміну графіка з кнопкою на сайт."""
    payload = json.dumps({
        "chat_id": CHANNEL_ID,
        "text": message,
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                {
                    "text": "⚡ Переглянути графік",
                    "url": SITE_URL,
                }
            ]]
        },
    }).encode("utf-8")
    telegram_request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        payload,
        {"Content-Type": "application/json"},
        "Повідомлення про оновлення графіка",
        timeout=35,
    )
    print("Telegram: повідомлення про оновлення графіка успішно надіслано")


def send_message(message):
    payload = json.dumps({
        "chat_id": CHANNEL_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    telegram_request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        payload,
        {"Content-Type": "application/json"},
        "Telegram-повідомлення",
        timeout=35,
    )
    print("Telegram: погодинне повідомлення успішно надіслано")

def publish_hourly_report(state, schedule, now):
    # The workflow runs every five minutes. Publish once during the first
    # 15 minutes of each hour, avoiding duplicate posts across workflow runs.
    if now.minute >= 15:
        return
    report_key = now.strftime("%Y-%m-%d-%H")
    reports = state.setdefault("hourlyReports", {})
    if reports.get("lastSent") == report_key:
        return
    message = build_hourly_message(schedule, now)
    send_message(message)
    reports["lastSent"] = report_key
    save_state(state)


def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задано — Telegram-публікація неможлива.")
    now = datetime.now(KYIV)
    today = now.strftime("%Y-%m-%d")
    from datetime import timedelta
    tomorrow_key = (now.date() + timedelta(days=1)).isoformat()

    state = load_state()
    entries = state.setdefault("entries", {})

    today_schedule = load_schedule(today)

    # Окремий workflow відповідає тільки за повідомлення кожні 30 хвилин.
    # Основний workflow не надсилає їх, щоб паралельні запуски не дублювали пости.
    if "--status-only" in sys.argv:
        # GitHub Actions can start scheduled runs after their intended time.
        # Allow delayed starts until the next :00 or :30 boundary.
        # Forecast content is for the following half-hour interval.
        test_pending = (
            os.environ.get("TELEGRAM_SKIP_TESTS") != "1"
            and Path("scripts/telegram-test-pending.txt").exists()
            and state.get("telegramTests", {}).get("lastTestId")
            != Path("scripts/telegram-test-pending.txt").read_text(encoding="utf-8").strip()
        )
        # Не обмежуємося першими 10 хвилинами після межі.
        # GitHub Actions може запустити workflow із затримкою; lastKey
        # нижче гарантує, що кожен півгодинний прогноз буде надіслано лише раз.
        in_status_window = True
        if not in_status_window and not test_pending:
            print(
                f"Поза вікном публікації ({now:%H:%M}); "
                "повідомлення буде надіслано після початку наступного інтервалу."
            )
            return
        if not today_schedule:
            raise RuntimeError(f"Немає актуального графіка на сьогодні ({today})")
        send_test_messages(today_schedule, state, now)
        if in_status_window:
            send_status_message(today_schedule, state, now)
            cleanup_status_messages(state, datetime.now(KYIV))
        print("Режим status-only: перевірку зображень пропущено.")
        return

    for date_key, kind in ((today, "today"), (tomorrow_key, "tomorrow")):
        schedule = load_schedule(date_key)
        if not schedule:
            continue
        queues = schedule["queues"]
        serialized = json.dumps({"queues": queues, "imageVersion": 11}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        previous_entry = entries.get(date_key)
        if previous_entry and previous_entry.get("hash") == digest:
            print(f"{date_key}: без змін")
            continue
        previous_queues = previous_entry.get("queues") if previous_entry else None
        message = build_caption(date_key, kind, previous_queues, queues)
        try:
            send_schedule_update(message)
        except Exception as error:
            print(f"Не вдалося надіслати повідомлення про графік {date_key}: {error}", file=sys.stderr)
            raise
        entries[date_key] = {"hash": digest, "queues": queues, "kind": kind, "sentAt": now.isoformat()}
        save_state(state)

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Помилка Telegram-публікації: {error}", file=sys.stderr)
        raise
