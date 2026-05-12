import os
import json
import requests
from datetime import datetime, date, timedelta

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

LATITUDE = float(os.environ.get("LATITUDE", "35.681236"))
LONGITUDE = float(os.environ.get("LONGITUDE", "139.767125"))
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tokyo")


@app.context_processor
def inject_now():
    return {"now": datetime.now}


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
    return date.today().isoformat()


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


# =========================
# 気圧取得
# =========================
def fetch_weather_from_open_meteo():
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": "pressure_msl,surface_pressure,temperature_2m,weather_code",
        "forecast_days": 1,
        "past_days": 1,
        "timezone": TIMEZONE,
    }

    res = requests.get(url, params=params, timeout=8)
    res.raise_for_status()
    data = res.json()

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    pressure_msl = hourly.get("pressure_msl", [])
    surface_pressure = hourly.get("surface_pressure", [])
    temperature = hourly.get("temperature_2m", [])
    weather_code = hourly.get("weather_code", [])

    if not times:
        raise RuntimeError("Open-Meteoから気圧データを取得できませんでした")

    now_dt = datetime.now()
    best_index = 0
    best_diff = None

    for i, t in enumerate(times):
        dt = datetime.fromisoformat(t)
        diff = abs((dt - now_dt).total_seconds())

        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_index = i

    logged_at = datetime.fromisoformat(times[best_index])
    current_pressure = pressure_msl[best_index]

    pressure_3h = None
    pressure_6h = None

    if best_index >= 3:
        pressure_3h = round(current_pressure - pressure_msl[best_index - 3], 2)

    if best_index >= 6:
        pressure_6h = round(current_pressure - pressure_msl[best_index - 6], 2)

    return {
        "logged_at": logged_at,
        "pressure_msl": current_pressure,
        "surface_pressure": surface_pressure[best_index],
        "pressure_change_3h": pressure_3h,
        "pressure_change_6h": pressure_6h,
        "temperature": temperature[best_index],
        "weather_code": weather_code[best_index],
    }


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
        weather_data = fetch_weather_from_open_meteo()
        save_weather_log(weather_data)
        return jsonify({"ok": True, "weather": weather_data})
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
    today = date.today()

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
    )


# =========================
# 嘔吐ログ
# =========================
@app.route("/vomit", methods=["GET", "POST"])
def vomit():
    if request.method == "POST":
        vomited_at = request.form.get("vomited_at") or datetime.now().strftime("%Y-%m-%dT%H:%M")
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
    """, (datetime.now(), "未入力", ""))

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
        """, (datetime.now(), request.form.get("memo", "")))

    return redirect(url_for("index"))


@app.route("/cycle/end", methods=["POST"])
def cycle_end():
    execute("""
        UPDATE cycle_modes
        SET ended_at = %s
        WHERE ended_at IS NULL
    """, (datetime.now(),))

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
    log_date = date.today()

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
        datetime.now() if status == "taken" else None,
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
        logged_at = request.form.get("logged_at") or datetime.now().strftime("%Y-%m-%dT%H:%M")

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
            w.temperature
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
        slept_at = request.form.get("slept_at") or datetime.now().strftime("%Y-%m-%dT%H:%M")
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
    """, (datetime.now(), "未入力", "", "", ""))

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
    today = date.today()

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
        change_3h = latest_weather.get("pressure_change_3h")
        change_6h = latest_weather.get("pressure_change_6h")

        if change_6h is not None and change_6h <= -5:
            alerts.append({
                "level": "danger",
                "title": "気圧が大きく下がっています",
                "message": f"6時間で{change_6h}hPa変化しています。眠気・頭痛・嘔吐に注意してください。"
            })
        elif change_3h is not None and change_3h <= -3:
            alerts.append({
                "level": "warning",
                "title": "気圧低下に注意",
                "message": f"3時間で{change_3h}hPa変化しています。無理せず様子見推奨です。"
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
            change_3h = latest_weather.get("pressure_change_3h")
            change_6h = latest_weather.get("pressure_change_6h")
            pressure_drop = (
                (change_3h is not None and change_3h <= -3)
                or (change_6h is not None and change_6h <= -5)
            )

            if pressure_drop and (
                max_sleepiness >= 4
                or max_headache >= 4
                or max_nausea >= 4
                or max_vomit_feeling >= 4
            ):
                alerts.append({
                    "level": "danger",
                    "title": "気圧低下＋体調悪化サイン",
                    "message": "気圧低下と体調悪化が同時に出ています。今日は無理しない優先日にしてください。"
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
    start_date = date.today() - timedelta(days=days - 1)

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
            w.pressure_change_6h
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
        weather_data = fetch_weather_from_open_meteo()
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
    app.run(debug=True)