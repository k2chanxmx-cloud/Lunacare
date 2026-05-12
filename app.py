import os
import json
import requests
from datetime import datetime, date, timedelta, timezone

import psycopg2
import psycopg2.extras
from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, flash, g
)
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")

DATABASE_URL = os.environ.get("DATABASE_URL")

JST = timezone(timedelta(hours=9))

# Zutool 気圧API用
KIATSU_AREA_CODE = os.environ.get("KIATSU_AREA_CODE", "13108")  # 江東区
KIATSU_AREA_NAME = os.environ.get("KIATSU_AREA_NAME", "東京・江東区")


PRESSURE_LEVEL = {
    "0": "通常",
    "1": "通常",
    "2": "やや注意",
    "3": "注意",
    "4": "警戒",
}


def now_jst():
    return datetime.now(JST)


@app.context_processor
def inject_now():
    return {"now": lambda: now_jst().replace(tzinfo=None)}


# =========================
# DB共通：1リクエスト1接続
# =========================
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL が設定されていません")

    if "db_conn" not in g:
        g.db_conn = psycopg2.connect(DATABASE_URL)

    return g.db_conn


@app.teardown_appcontext
def close_db_conn(exception=None):
    conn = g.pop("db_conn", None)
    if conn is not None:
        conn.close()


def query_all(sql, params=None):
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def query_one(sql, params=None):
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()


def execute(sql, params=None):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        conn.commit()


# =========================
# 共通
# =========================
def today_str():
    return now_jst().date().isoformat()


def calc_heart_display(score):
    try:
        score = int(score)
    except Exception:
        score = 3
    score = max(1, min(score, 5))
    return "♥" * score + "♡" * (5 - score)


def safe_int(value):
    try:
        if value in ("", None):
            return None
        return int(value)
    except Exception:
        return None


def safe_float(value):
    try:
        if value in ("", None, "-"):
            return None
        return float(value)
    except Exception:
        return None


# =========================
# 気圧取得：Zutool API版
# =========================
def fetch_kiatsu_from_zutool():
    api_url = f"https://zutool.jp/api/getweatherstatus/{KIATSU_AREA_CODE}"

    res = requests.get(api_url, timeout=8)
    res.raise_for_status()
    return res.json()


def pick_current_kiatsu_item(data):
    today_items = data.get("today", [])
    tomorrow_items = data.get("tommorow", data.get("tomorrow", []))

    now = now_jst()
    now_hour = now.hour

    candidates = []

    for item in today_items:
        try:
            hour = int(item.get("time", 0))
            diff = abs(hour - now_hour)
            candidates.append({
                "date": now.date(),
                "hour": hour,
                "diff": diff,
                "item": item
            })
        except Exception:
            pass

    if not candidates and tomorrow_items:
        tomorrow = now.date() + timedelta(days=1)
        for item in tomorrow_items:
            try:
                hour = int(item.get("time", 0))
                candidates.append({
                    "date": tomorrow,
                    "hour": hour,
                    "diff": 999 + hour,
                    "item": item
                })
            except Exception:
                pass

    if not candidates:
        raise RuntimeError("Zutoolから気圧データを取得できませんでした")

    picked = sorted(candidates, key=lambda x: x["diff"])[0]

    logged_at = datetime.combine(
        picked["date"],
        datetime.min.time()
    ).replace(hour=picked["hour"])

    item = picked["item"]

    pressure = safe_float(item.get("pressure"))
    temp = safe_float(item.get("temp"))
    level_code = str(item.get("pressure_level", "0"))

    return {
        "logged_at": logged_at,
        "pressure_msl": pressure,
        "surface_pressure": pressure,
        "pressure_change_3h": None,
        "pressure_change_6h": None,
        "temperature": temp,
        "weather_code": safe_int(level_code),
        "pressure_level": level_code,
        "pressure_level_label": PRESSURE_LEVEL.get(level_code, "通常"),
        "raw": item,
    }


def fetch_weather_from_zutool():
    data = fetch_kiatsu_from_zutool()
    return pick_current_kiatsu_item(data)


def save_weather_log(weather):
    execute("""
        INSERT INTO weather_logs
            (
                logged_at,
                pressure_msl,
                surface_pressure,
                pressure_change_3h,
                pressure_change_6h,
                temperature,
                weather_code
            )
        VALUES
            (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (logged_at)
        DO UPDATE SET
            pressure_msl = EXCLUDED.pressure_msl,
            surface_pressure = EXCLUDED.surface_pressure,
            pressure_change_3h = EXCLUDED.pressure_change_3h,
            pressure_change_6h = EXCLUDED.pressure_change_6h,
            temperature = EXCLUDED.temperature,
            weather_code = EXCLUDED.weather_code,
            created_at = NOW()
    """, (
        weather["logged_at"],
        weather["pressure_msl"],
        weather["surface_pressure"],
        weather["pressure_change_3h"],
        weather["pressure_change_6h"],
        weather["temperature"],
        weather["weather_code"],
    ))


@app.route("/weather/update")
def weather_update():
    try:
        weather_data = fetch_weather_from_zutool()
        save_weather_log(weather_data)
        return jsonify({
            "ok": True,
            "source": "zutool",
            "area": KIATSU_AREA_NAME,
            "weather": weather_data
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/weather")
def weather():
    logs = query_all("""
        SELECT *
        FROM weather_logs
        ORDER BY logged_at DESC
        LIMIT 72
    """)

    latest = logs[0] if logs else None

    return render_template(
        "weather.html",
        logs=logs,
        latest=latest,
    )


# =========================
# ホーム
# =========================
@app.route("/")
def index():
    today = now_jst().date()

    vomit_count = query_one("""
        SELECT COUNT(*) AS count
        FROM vomit_logs
        WHERE DATE(vomited_at) = %s
    """, (today,))["count"]

    sleep_count = query_one("""
        SELECT COUNT(*) AS count
        FROM work_sleep_logs
        WHERE DATE(slept_at) = %s
    """, (today,))["count"]

    active_cycle = query_one("""
        SELECT *
        FROM cycle_modes
        WHERE ended_at IS NULL
        ORDER BY started_at DESC
        LIMIT 1
    """)

    diary = query_one("""
        SELECT *
        FROM diary_logs
        WHERE log_date = %s
        ORDER BY created_at DESC
        LIMIT 1
    """, (today,))

    condition = query_one("""
        SELECT *
        FROM body_condition_logs
        WHERE DATE(logged_at) = %s
        ORDER BY logged_at DESC, created_at DESC
        LIMIT 1
    """, (today,))

    condition_count_today = query_one("""
        SELECT COUNT(*) AS count
        FROM body_condition_logs
        WHERE DATE(logged_at) = %s
    """, (today,))["count"]

    weather_latest = query_one("""
        SELECT *
        FROM weather_logs
        ORDER BY logged_at DESC
        LIMIT 1
    """)

    medicines = query_all("""
        SELECT *
        FROM medicines
        WHERE is_active = TRUE
        ORDER BY sort_order, take_time, id
    """)

    today_med_logs = query_all("""
        SELECT medicine_id, status
        FROM medicine_logs
        WHERE log_date = %s
    """, (today,))

    med_status = {row["medicine_id"]: row["status"] for row in today_med_logs}

    alerts = check_alerts()

    return render_template(
        "index.html",
        today=today,
        vomit_count=vomit_count,
        sleep_count=sleep_count,
        active_cycle=active_cycle,
        diary=diary,
        heart_display=calc_heart_display(diary["heart_score"]) if diary else "♡♡♡♡♡",
        condition=condition,
        condition_count_today=condition_count_today,
        weather_latest=weather_latest,
        medicines=medicines,
        med_status=med_status,
        alerts=alerts,
        pressure_level_map=PRESSURE_LEVEL,
    )


# =========================
# 嘔吐ログ
# =========================
@app.route("/vomit", methods=["GET", "POST"])
def vomit():
    if request.method == "POST":
        vomited_at = request.form.get("vomited_at") or now_jst().replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M")
        food = request.form.get("food", "")
        trigger = request.form.get("trigger", "")
        memo = request.form.get("memo", "")

        execute("""
            INSERT INTO vomit_logs
                (vomited_at, food, trigger, memo)
            VALUES
                (%s, %s, %s, %s)
        """, (vomited_at, food, trigger, memo))

        flash("嘔吐ログを記録しました")
        return redirect(url_for("vomit"))

    logs = query_all("""
        SELECT *
        FROM vomit_logs
        ORDER BY vomited_at DESC
        LIMIT 100
    """)

    return render_template("vomit.html", logs=logs)


@app.route("/vomit/quick", methods=["POST"])
def vomit_quick():
    execute("""
        INSERT INTO vomit_logs
            (vomited_at, trigger, memo)
        VALUES
            (%s, %s, %s)
    """, (now_jst().replace(tzinfo=None), "未入力", ""))

    return redirect(url_for("index"))


@app.route("/vomit/delete/<int:log_id>", methods=["POST"])
def delete_vomit(log_id):
    execute("DELETE FROM vomit_logs WHERE id = %s", (log_id,))
    flash("嘔吐ログを削除しました")
    return redirect(url_for("vomit"))


# =========================
# 生理前モード
# =========================
@app.route("/cycle/start", methods=["POST"])
def cycle_start():
    active = query_one("""
        SELECT id
        FROM cycle_modes
        WHERE ended_at IS NULL
        LIMIT 1
    """)

    if not active:
        execute("""
            INSERT INTO cycle_modes
                (started_at, memo)
            VALUES
                (%s, %s)
        """, (now_jst().replace(tzinfo=None), request.form.get("memo", "")))

    return redirect(url_for("index"))


@app.route("/cycle/end", methods=["POST"])
def cycle_end():
    execute("""
        UPDATE cycle_modes
        SET ended_at = %s
        WHERE ended_at IS NULL
    """, (now_jst().replace(tzinfo=None),))

    return redirect(url_for("index"))


# =========================
# 薬マスタ・服薬
# =========================
@app.route("/medicines", methods=["GET", "POST"])
def medicines():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        dose = request.form.get("dose", "").strip()
        take_time = request.form.get("take_time") or None
        timing_label = request.form.get("timing_label", "").strip()
        memo = request.form.get("memo", "").strip()

        if name:
            execute("""
                INSERT INTO medicines
                    (name, dose, take_time, timing_label, memo, is_active)
                VALUES
                    (%s, %s, %s, %s, %s, TRUE)
            """, (name, dose, take_time, timing_label, memo))

            flash("薬を追加しました")

        return redirect(url_for("medicines"))

    meds = query_all("""
        SELECT *
        FROM medicines
        ORDER BY is_active DESC, sort_order, take_time, id
    """)

    return render_template("medicines.html", medicines=meds)


@app.route("/medicines/toggle/<int:medicine_id>", methods=["POST"])
def medicine_toggle(medicine_id):
    execute("""
        UPDATE medicines
        SET is_active = NOT is_active
        WHERE id = %s
    """, (medicine_id,))
    return redirect(url_for("medicines"))


@app.route("/medicines/delete/<int:medicine_id>", methods=["POST"])
def medicine_delete(medicine_id):
    execute("DELETE FROM medicines WHERE id = %s", (medicine_id,))
    flash("薬を削除しました")
    return redirect(url_for("medicines"))


@app.route("/medicine/taken/<int:medicine_id>", methods=["POST"])
def medicine_taken(medicine_id):
    status = request.form.get("status", "taken")
    log_date = now_jst().date()

    execute("""
        INSERT INTO medicine_logs
            (medicine_id, log_date, status, taken_at, memo)
        VALUES
            (%s, %s, %s, %s, %s)
        ON CONFLICT (medicine_id, log_date)
        DO UPDATE SET
            status = EXCLUDED.status,
            taken_at = EXCLUDED.taken_at,
            memo = EXCLUDED.memo
    """, (
        medicine_id,
        log_date,
        status,
        now_jst().replace(tzinfo=None) if status == "taken" else None,
        request.form.get("memo", "")
    ))

    return redirect(request.referrer or url_for("index"))


# =========================
# 日記・こころログ
# =========================
@app.route("/diary", methods=["GET", "POST"])
def diary():
    if request.method == "POST":
        log_date = request.form.get("log_date") or today_str()
        heart_score = int(request.form.get("heart_score", 3))
        mood_tag = request.form.get("mood_tag", "")
        memo = request.form.get("memo", "")

        execute("""
            INSERT INTO diary_logs
                (log_date, heart_score, mood_tag, memo)
            VALUES
                (%s, %s, %s, %s)
        """, (log_date, heart_score, mood_tag, memo))

        flash("こころログを保存しました")
        return redirect(url_for("diary"))

    logs = query_all("""
        SELECT *
        FROM diary_logs
        ORDER BY log_date DESC, created_at DESC
        LIMIT 100
    """)

    for row in logs:
        row["heart_display"] = calc_heart_display(row["heart_score"])

    return render_template("diary.html", logs=logs, today=today_str())


@app.route("/diary/delete/<int:log_id>", methods=["POST"])
def diary_delete(log_id):
    execute("DELETE FROM diary_logs WHERE id = %s", (log_id,))
    flash("こころログを削除しました")
    return redirect(url_for("diary"))


# =========================
# 体調ログ
# =========================
@app.route("/condition", methods=["GET", "POST"])
def condition():
    if request.method == "POST":
        logged_at = request.form.get("logged_at") or now_jst().replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M")

        sleepiness_level = safe_int(request.form.get("sleepiness_level"))
        headache_level = safe_int(request.form.get("headache_level"))
        nausea_level = safe_int(request.form.get("nausea_level"))
        vomit_feeling_level = safe_int(request.form.get("vomit_feeling_level"))
        memo = request.form.get("memo", "")

        latest_weather = query_one("""
            SELECT id
            FROM weather_logs
            ORDER BY logged_at DESC
            LIMIT 1
        """)

        weather_log_id = latest_weather["id"] if latest_weather else None

        execute("""
            INSERT INTO body_condition_logs
                (
                    logged_at,
                    log_date,
                    sleepiness_level,
                    headache_level,
                    nausea_level,
                    vomit_feeling_level,
                    weather_log_id,
                    memo
                )
            VALUES
                (%s, DATE(%s), %s, %s, %s, %s, %s, %s)
        """, (
            logged_at,
            logged_at,
            sleepiness_level,
            headache_level,
            nausea_level,
            vomit_feeling_level,
            weather_log_id,
            memo
        ))

        flash("体調ログを保存しました")
        return redirect(url_for("condition"))

    logs = query_all("""
        SELECT
            c.*,
            w.pressure_msl,
            w.pressure_change_3h,
            w.pressure_change_6h,
            w.temperature,
            w.weather_code
        FROM body_condition_logs c
        LEFT JOIN weather_logs w
            ON c.weather_log_id = w.id
        ORDER BY c.logged_at DESC, c.created_at DESC
        LIMIT 100
    """)

    latest_weather = query_one("""
        SELECT *
        FROM weather_logs
        ORDER BY logged_at DESC
        LIMIT 1
    """)

    return render_template(
        "condition.html",
        logs=logs,
        today=today_str(),
        latest_weather=latest_weather,
        pressure_level_map=PRESSURE_LEVEL,
    )


@app.route("/condition/delete/<int:log_id>", methods=["POST"])
def condition_delete(log_id):
    execute("DELETE FROM body_condition_logs WHERE id = %s", (log_id,))
    flash("体調ログを削除しました")
    return redirect(url_for("condition"))


# =========================
# 仕事中睡眠ログ
# =========================
@app.route("/work-sleep", methods=["GET", "POST"])
def work_sleep():
    if request.method == "POST":
        slept_at = request.form.get("slept_at") or now_jst().replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M")
        duration_minutes = request.form.get("duration_minutes") or None
        trigger = request.form.get("trigger", "")
        escape_reason = request.form.get("escape_reason", "")
        workload_level = request.form.get("workload_level", "")
        memo = request.form.get("memo", "")

        execute("""
            INSERT INTO work_sleep_logs
                (slept_at, duration_minutes, trigger, escape_reason, workload_level, memo)
            VALUES
                (%s, %s, %s, %s, %s, %s)
        """, (
            slept_at,
            duration_minutes,
            trigger,
            escape_reason,
            workload_level,
            memo
        ))

        flash("仕事中睡眠ログを記録しました")
        return redirect(url_for("work_sleep"))

    logs = query_all("""
        SELECT *
        FROM work_sleep_logs
        ORDER BY slept_at DESC
        LIMIT 100
    """)

    return render_template("work_sleep.html", logs=logs)


@app.route("/work-sleep/quick", methods=["POST"])
def work_sleep_quick():
    execute("""
        INSERT INTO work_sleep_logs
            (slept_at, trigger, escape_reason, workload_level, memo)
        VALUES
            (%s, %s, %s, %s, %s)
    """, (now_jst().replace(tzinfo=None), "未入力", "", "", ""))

    return redirect(url_for("index"))


@app.route("/work-sleep/delete/<int:log_id>", methods=["POST"])
def work_sleep_delete(log_id):
    execute("DELETE FROM work_sleep_logs WHERE id = %s", (log_id,))
    flash("仕事中睡眠ログを削除しました")
    return redirect(url_for("work_sleep"))


# =========================
# アラート判定
# =========================
def check_alerts():
    today = now_jst().date()

    vomit_count = query_one("""
        SELECT COUNT(*) AS count
        FROM vomit_logs
        WHERE DATE(vomited_at) = %s
    """, (today,))["count"]

    sleep_count = query_one("""
        SELECT COUNT(*) AS count
        FROM work_sleep_logs
        WHERE DATE(slept_at) = %s
    """, (today,))["count"]

    active_cycle = query_one("""
        SELECT id
        FROM cycle_modes
        WHERE ended_at IS NULL
        LIMIT 1
    """)

    missed_meds = query_one("""
        SELECT COUNT(*) AS count
        FROM medicine_logs
        WHERE log_date = %s
          AND status IN ('skip', 'missed')
    """, (today,))["count"]

    latest_diary = query_one("""
        SELECT heart_score
        FROM diary_logs
        WHERE log_date = %s
        ORDER BY created_at DESC
        LIMIT 1
    """, (today,))

    latest_weather = query_one("""
        SELECT *
        FROM weather_logs
        ORDER BY logged_at DESC
        LIMIT 1
    """)

    today_condition_max = query_one("""
        SELECT
            MAX(sleepiness_level) AS max_sleepiness,
            MAX(headache_level) AS max_headache,
            MAX(nausea_level) AS max_nausea,
            MAX(vomit_feeling_level) AS max_vomit_feeling
        FROM body_condition_logs
        WHERE DATE(logged_at) = %s
    """, (today,))

    alerts = []

    if vomit_count >= 5:
        alerts.append({
            "level": "danger",
            "title": "嘔吐回数が危険ラインです",
            "message": "今日の嘔吐が5回以上です。入院回避のため、水分補給・休息・相談を優先してください。"
        })
    elif vomit_count >= 3:
        alerts.append({
            "level": "warning",
            "title": "嘔吐回数が増えています",
            "message": "今日の嘔吐が3回以上です。無理せず、早めに休むラインです。"
        })

    if active_cycle and vomit_count >= 2:
        alerts.append({
            "level": "danger",
            "title": "生理前モード中の嘔吐増加",
            "message": "生理前モード中に嘔吐が増えています。爆食い・嘔吐の悪化前に対策したい状態です。"
        })

    if sleep_count >= 2:
        alerts.append({
            "level": "warning",
            "title": "仕事中睡眠が増えています",
            "message": "仕事中に2回以上寝ています。逃げたくなるほど負荷が高いサインかもしれません。"
        })

    if missed_meds >= 2:
        alerts.append({
            "level": "warning",
            "title": "薬の飲み忘れが増えています",
            "message": "薬のスキップ・飲み忘れが2回以上あります。服薬リズムを確認してください。"
        })

    if latest_diary and latest_diary["heart_score"] <= 2:
        alerts.append({
            "level": "warning",
            "title": "こころスコアが低めです",
            "message": "今日のこころログが低めです。無理に通常運転しなくて大丈夫です。"
        })

    if vomit_count >= 2 and sleep_count >= 1:
        alerts.append({
            "level": "danger",
            "title": "嘔吐＋仕事中睡眠が同日に出ています",
            "message": "身体と心の両方に負荷が出ています。入院回避のため、早めに休む・相談する候補です。"
        })

    if latest_weather:
        level = latest_weather.get("weather_code")

        if level is not None:
            try:
                level = int(level)
            except Exception:
                level = 0

            if level >= 4:
                alerts.append({
                    "level": "danger",
                    "title": "気圧が警戒レベルです",
                    "message": "気圧レベルが警戒です。眠気・頭痛・嘔吐に注意してください。"
                })
            elif level >= 3:
                alerts.append({
                    "level": "warning",
                    "title": "気圧が注意レベルです",
                    "message": "気圧レベルが注意です。無理せず様子見推奨です。"
                })
            elif level >= 2:
                alerts.append({
                    "level": "warning",
                    "title": "気圧がやや注意です",
                    "message": "少し気圧変化があります。眠気・頭痛に注意してください。"
                })

    if today_condition_max:
        max_sleepiness = today_condition_max.get("max_sleepiness") or 0
        max_headache = today_condition_max.get("max_headache") or 0
        max_nausea = today_condition_max.get("max_nausea") or 0
        max_vomit_feeling = today_condition_max.get("max_vomit_feeling") or 0

        if max_sleepiness >= 4:
            alerts.append({
                "level": "warning",
                "title": "今日は眠気が強い時間帯があります",
                "message": "眠気レベル4以上の記録があります。気圧・薬・精神負荷の影響も見返せます。"
            })

        if max_headache >= 4:
            alerts.append({
                "level": "warning",
                "title": "今日は頭痛が強い時間帯があります",
                "message": "頭痛レベル4以上の記録があります。気圧変化との関連を確認できます。"
            })

        if max_nausea >= 4 or max_vomit_feeling >= 4:
            alerts.append({
                "level": "danger",
                "title": "吐き気・嘔吐感が強い時間帯があります",
                "message": "吐き気または嘔吐感が強めです。嘔吐回数が増える前に、休息・水分・相談を優先してください。"
            })

        if latest_weather:
            try:
                pressure_level = int(latest_weather.get("weather_code") or 0)
            except Exception:
                pressure_level = 0

            if pressure_level >= 3 and (
                max_sleepiness >= 4
                or max_headache >= 4
                or max_nausea >= 4
                or max_vomit_feeling >= 4
            ):
                alerts.append({
                    "level": "danger",
                    "title": "気圧注意＋体調悪化サイン",
                    "message": "気圧注意と体調悪化が同時に出ています。今日は無理しない優先日にしてください。"
                })

    return alerts


@app.route("/api/alerts")
def api_alerts():
    return jsonify(check_alerts())


# =========================
# レポート
# =========================
@app.route("/report")
def report():
    days = int(request.args.get("days", 30))
    start_date = now_jst().date() - timedelta(days=days - 1)

    vomit_summary = query_one("""
        SELECT COUNT(*) AS total
        FROM vomit_logs
        WHERE DATE(vomited_at) >= %s
    """, (start_date,))

    sleep_summary = query_one("""
        SELECT COUNT(*) AS total
        FROM work_sleep_logs
        WHERE DATE(slept_at) >= %s
    """, (start_date,))

    diary_summary = query_one("""
        SELECT
            COUNT(*) AS count,
            ROUND(AVG(heart_score)::numeric, 2) AS avg_score,
            MIN(heart_score) AS min_score
        FROM diary_logs
        WHERE log_date >= %s
    """, (start_date,))

    condition_summary = query_one("""
        SELECT
            COUNT(*) AS count,
            ROUND(AVG(sleepiness_level)::numeric, 2) AS avg_sleepiness,
            ROUND(AVG(headache_level)::numeric, 2) AS avg_headache,
            ROUND(AVG(nausea_level)::numeric, 2) AS avg_nausea,
            ROUND(AVG(vomit_feeling_level)::numeric, 2) AS avg_vomit_feeling,
            MAX(sleepiness_level) AS max_sleepiness,
            MAX(headache_level) AS max_headache,
            MAX(nausea_level) AS max_nausea,
            MAX(vomit_feeling_level) AS max_vomit_feeling
        FROM body_condition_logs
        WHERE DATE(logged_at) >= %s
    """, (start_date,))

    med_summary = query_all("""
        SELECT
            m.name,
            COUNT(l.id) FILTER (WHERE l.status = 'taken') AS taken_count,
            COUNT(l.id) FILTER (WHERE l.status IN ('skip', 'missed')) AS missed_count
        FROM medicines m
        LEFT JOIN medicine_logs l
            ON m.id = l.medicine_id
           AND l.log_date >= %s
        GROUP BY m.id, m.name
        ORDER BY m.sort_order, m.id
    """, (start_date,))

    recent_vomits = query_all("""
        SELECT *
        FROM vomit_logs
        WHERE DATE(vomited_at) >= %s
        ORDER BY vomited_at DESC
        LIMIT 20
    """, (start_date,))

    recent_diaries = query_all("""
        SELECT *
        FROM diary_logs
        WHERE log_date >= %s
        ORDER BY log_date DESC, created_at DESC
        LIMIT 20
    """, (start_date,))

    recent_conditions = query_all("""
        SELECT
            c.*,
            w.pressure_msl,
            w.pressure_change_3h,
            w.pressure_change_6h,
            w.weather_code
        FROM body_condition_logs c
        LEFT JOIN weather_logs w
            ON c.weather_log_id = w.id
        WHERE DATE(c.logged_at) >= %s
        ORDER BY c.logged_at DESC, c.created_at DESC
        LIMIT 20
    """, (start_date,))

    for row in recent_diaries:
        row["heart_display"] = calc_heart_display(row["heart_score"])

    return render_template(
        "report.html",
        days=days,
        start_date=start_date,
        vomit_summary=vomit_summary,
        sleep_summary=sleep_summary,
        diary_summary=diary_summary,
        condition_summary=condition_summary,
        med_summary=med_summary,
        recent_vomits=recent_vomits,
        recent_diaries=recent_diaries,
        recent_conditions=recent_conditions,
        pressure_level_map=PRESSURE_LEVEL,
    )


# =========================
# Push通知 土台
# =========================
@app.route("/push/subscribe", methods=["POST"])
def push_subscribe():
    data = request.get_json()

    if not data:
        return jsonify({"ok": False, "error": "no data"}), 400

    endpoint = data.get("endpoint")

    execute("""
        INSERT INTO push_subscriptions
            (endpoint, subscription_json)
        VALUES
            (%s, %s)
        ON CONFLICT (endpoint)
        DO UPDATE SET
            subscription_json = EXCLUDED.subscription_json,
            updated_at = NOW()
    """, (endpoint, json.dumps(data)))

    return jsonify({"ok": True})


@app.route("/notification-check")
def notification_check():
    weather_result = None
    weather_error = None

    try:
        weather_data = fetch_weather_from_zutool()
        save_weather_log(weather_data)
        weather_result = weather_data
    except Exception as e:
        weather_error = str(e)

    alerts = check_alerts()

    return jsonify({
        "ok": True,
        "date": today_str(),
        "weather": weather_result,
        "weather_error": weather_error,
        "alert_count": len(alerts),
        "alerts": alerts
    })


# =========================
# PWA用
# =========================
@app.route("/manifest.json")
def manifest():
    return app.send_static_file("manifest.json")


@app.route("/sw.js")
def service_worker():
    response = app.send_static_file("sw.js")
    response.headers["Content-Type"] = "application/javascript"
    return response


@app.route("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)