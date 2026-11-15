"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import datetime
import json
import secrets
import threading
import time
from pathlib import Path

from . import accounts, alerts, auth, config, creators, ctx, jobs, live

# IST timezone: UTC + 5:30
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Weekly upload slots (hour, minute) in IST:
# Full schedule (8 shorts/day):
# Monday–Thursday: 8:00 AM, 10:00 AM, 12:00 PM, 2:00 PM, 4:30 PM, 6:30 PM, 8:30 PM, 10:00 PM
MON_THU_SLOTS = [(8, 0), (10, 0), (12, 0), (14, 0), (16, 30), (18, 30), (20, 30), (22, 0)]
# Friday: Same as Monday–Thursday, but the last slot is 10:30 PM
FRI_SLOTS = [(8, 0), (10, 0), (12, 0), (14, 0), (16, 30), (18, 30), (20, 30), (22, 30)]
# Saturday–Sunday: 9:30 AM, 11:30 AM, 1:30 PM, 3:30 PM, 5:30 PM, 7:30 PM, 9:30 PM, 11:00 PM
WEEKEND_SLOTS = [(9, 30), (11, 30), (13, 30), (15, 30), (17, 30), (19, 30), (21, 30), (23, 0)]

# First 3 weeks schedule (4 shorts/day, prime peak times) in IST:
# Monday–Thursday: 10:00 AM, 2:00 PM, 6:30 PM, 10:00 PM
MON_THU_4_SLOTS = [(10, 0), (14, 0), (18, 30), (22, 0)]
# Friday: 10:00 AM, 2:00 PM, 6:30 PM, 10:30 PM
FRI_4_SLOTS = [(10, 0), (14, 0), (18, 30), (22, 30)]
# Saturday–Sunday: 11:30 AM, 3:30 PM, 7:30 PM, 11:00 PM
WEEKEND_4_SLOTS = [(11, 30), (15, 30), (19, 30), (23, 0)]

# Fixed schedule boundaries in IST:
# First 3 Weeks: Nov 15, 2026 to Dec 5, 2026 (4 shorts/day)
# Later: Dec 6, 2026 onwards (8 shorts/day)
START_DATE_3W = datetime.date(2026, 11, 15)
END_DATE_3W = datetime.date(2026, 12, 5)


def _schedule_start_date() -> datetime.date:
    return START_DATE_3W


def _is_first_3_weeks(target_date: datetime.date) -> bool:
    """True if target_date falls within the first 3 weeks (Nov 15, 2026 to Dec 5, 2026)."""
    return START_DATE_3W <= target_date <= END_DATE_3W


def _max_shorts_for_date(target_date: datetime.date) -> int:
    """4 shorts/day for the first 3 weeks, then 8 shorts/day later."""
    return 4 if _is_first_3_weeks(target_date) else 8


def _slots_for_date(target_date: datetime.date):
    """Return the daily slot times in IST based on whether the date is in the first 3 weeks or later."""
    wd = target_date.weekday()
    if _is_first_3_weeks(target_date):
        if wd < 4:
            return MON_THU_4_SLOTS
        if wd == 4:
            return FRI_4_SLOTS
        return WEEKEND_4_SLOTS
    else:
        if wd < 4:
            return MON_THU_SLOTS
        if wd == 4:
            return FRI_SLOTS
        return WEEKEND_SLOTS


def _slots_for_weekday(wd: int):
    """Legacy helper for weekday slots (8 slots)."""
    if wd < 4:
        return MON_THU_SLOTS
    if wd == 4:
        return FRI_SLOTS
    return WEEKEND_SLOTS


_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "posting")


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def _day(t):
    try:
        return datetime.datetime.fromtimestamp(t, tz=IST).strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url", "due_at", "error", "views", "likes", "comments")}
        jobs.save(job)


def _next_slot(account_id, gap_min=None, cap=None):
    """Earliest slot from the weekly schedule (IST) that is not already occupied for this account.
    First 3 weeks (Nov 15 to Dec 5, 2026): strictly 4 shorts/day. Any extra automatically rolls over to next day.
    Later (From Dec 6, 2026 onwards): strictly 8 shorts/day."""
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",)]
    taken = {round(p["due_at"]) for p in mine if p.get("due_at")}
    now_ts = time.time()
    now_ist = datetime.datetime.now(IST)

    # Search up to 90 days for the earliest free scheduled slot
    for day_offset in range(90):
        target_date = (now_ist + datetime.timedelta(days=day_offset)).date()
        slots = _slots_for_date(target_date)
        max_daily = _max_shorts_for_date(target_date)

        # Strictly enforce daily cap (4 in first 3 weeks, 8 later)
        day_str = target_date.strftime("%Y-%m-%d")
        posts_on_day = sum(1 for p in mine if _day(p.get("posted_at") or p.get("due_at", 0)) == day_str)
        if posts_on_day >= max_daily:
            continue

        for hour, minute in slots:
            slot_dt = datetime.datetime(
                target_date.year, target_date.month, target_date.day,
                hour, minute, 0, 0, tzinfo=IST
            )
            slot_ts = slot_dt.timestamp()

            # Skip slots in the past or within 30 seconds
            if slot_ts <= now_ts + 30:
                continue

            # Skip slots already occupied by another post for this account
            if any(abs(t - slot_ts) < 300 for t in taken):
                continue

            return round(slot_ts, 2)

    return round(now_ts + 3600, 2)


def upcoming_slots(account_id: str = None, count: int = 16) -> list:
    """Return the next available schedule slots in IST for selection, with the first free slot marked as default."""
    now_ts = time.time()
    now_ist = datetime.datetime.now(IST)
    mine = _mine()
    if account_id:
        mine = [p for p in mine if p.get("account") == account_id]
    taken = [p["due_at"] for p in mine if p.get("due_at") and p.get("status") in OPEN]

    results = []
    for day_offset in range(60):
        if len(results) >= count:
            break
        target_date = (now_ist + datetime.timedelta(days=day_offset)).date()
        slots = _slots_for_date(target_date)
        max_daily = _max_shorts_for_date(target_date)
        day_str = target_date.strftime("%Y-%m-%d")
        posts_on_day = sum(1 for p in mine if _day(p.get("posted_at") or p.get("due_at", 0)) == day_str)
        if posts_on_day >= max_daily:
            continue

        for hour, minute in slots:
            slot_dt = datetime.datetime(
                target_date.year, target_date.month, target_date.day,
                hour, minute, 0, 0, tzinfo=IST
            )
            slot_ts = slot_dt.timestamp()
            if slot_ts <= now_ts + 30:
                continue
            if any(abs(t - slot_ts) < 300 for t in taken):
                continue

            day_diff = (target_date - now_ist.date()).days
            if day_diff == 0:
                prefix = "Today"
            elif day_diff == 1:
                prefix = "Tomorrow"
            else:
                prefix = slot_dt.strftime("%a, %d %b")

            time_str = slot_dt.strftime("%I:%M %p")
            is_phase1 = target_date <= END_DATE_3W
            phase_label = "Phase 1: 4/day" if is_phase1 else "Phase 2: 8/day"

            results.append({
                "timestamp": round(slot_ts, 2),
                "iso": slot_dt.isoformat(),
                "time_str": time_str,
                "date_str": day_str,
                "label": f"{prefix} · {time_str} ({phase_label})",
                "short_label": f"{prefix} · {time_str}",
                "phase": 1 if is_phase1 else 2,
                "is_default": len(results) == 0,
            })
            if len(results) >= count:
                break

    return results


def realign_scheduled_queue():
    """Ensure all currently waiting unpinned scheduled posts respect the 4/day (first 3 weeks) or 8/day (later) rule and slot times."""
    with _lock:
        scheduled = [p for p in _posts if p.get("status") == "scheduled" and not p.get("pinned")]
        if not scheduled:
            return
        scheduled.sort(key=lambda p: (p.get("created") or p.get("due_at", 0)))
        # Temporarily clear their due_at so they can be reassigned in chronological order
        for p in scheduled:
            p["due_at"] = None
        for p in scheduled:
            user_id = p.get("user") or (auth.local_user().id if hasattr(auth, "local_user") else None)
            if user_id:
                with ctx.as_user(user_id):
                    p["due_at"] = _next_slot(p["account"])
                    _mirror(p)
            else:
                p["due_at"] = _next_slot(p["account"])
                _mirror(p)
        _save()


def enqueue(job, clip, account_ids, now=False, due_at=None) -> list:
    c = creators.get(job["creator"]) or creators.DEFAULTS
    made = []
    with _lock:
        for aid in account_ids:
            acc = accounts.get(aid)
            if not acc:
                continue
            if any(p["job"] == job["id"] and p["clip"] == clip["id"] and p["account"] == aid
                   and p["status"] in OPEN + ("posted",) for p in _mine()):
                continue  # never post the same clip to the same account twice
            if now:
                due = time.time()
                pinned = False
            elif due_at is not None:
                due = float(due_at)
                pinned = True
            else:
                due = _next_slot(aid)
                pinned = False
            p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job["creator"],
                 "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": "scheduled",
                 "due_at": due, "pinned": pinned, "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
                 "score": clip.get("score", 0), "tries": 0, "url": "", "remote_id": "", "error": None, "views": None}
            _posts.append(p)
            made.append(p)
        _save()
    for p in made:
        _mirror(p)
    if not now and made and due_at is None:
        realign_scheduled_queue()
    return made


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    c = creators.get(job["creator"])
    if not c or not c["autopost"] or not c["accounts"]:
        return
    if clip.get("score", 0) < c["post_min_score"]:
        return  # includes hand-made clips (score 0): those wait for you to press Post
    made = enqueue(job, clip, c["accounts"])
    if made:
        creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


def _caption(clip, creator):
    desc = (clip.get("description") or "").strip()
    tags = [h.lstrip("#") for h in clip.get("hashtags", [])]
    for req in ("viralsnips", "snipforge"):
        if not any(t.lower() == req for t in tags):
            tags.append(req)
    new_tags = [f"#{h}" for h in tags if f"#{h}".lower() not in desc.lower()]
    for req in ("#viralsnips", "#snipforge"):
        if req.lower() not in desc.lower() and req.lower() not in [x.lower() for x in new_tags]:
            new_tags.append(req)
    tags_str = " ".join(new_tags)
    footer = (creator.get("post_footer") or "").strip()
    return "\n\n".join(x for x in (desc, tags_str, footer) if x)


def _post(p):
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        if not acc:
            raise RuntimeError("That account was disconnected.")
        path = Path(job["dir"]) / "clips" / clip["file"]
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        title, cap = clip.get("title") or clip.get("hook") or "Clip", _caption(clip, creator)
        if acc["platform"] == "youtube":
            shorts_tag = "" if "#shorts" in cap.lower() else "\n\n#Shorts"
            rid, url = accounts.youtube_upload(acc, path, title, (cap + shorts_tag).strip(), clip.get("hashtags", []), creator["privacy"])
        elif acc["platform"] == "instagram":
            rid, url = accounts.instagram_upload(acc, path, f"{title}\n\n{cap}", job["id"])
        elif acc["platform"] == "tiktok":
            rid, url = accounts.tiktok_upload(acc, path, f"{title}\n\n{cap}", creator["privacy"])
        else:
            raise RuntimeError("That kind of account is no longer supported.")
        p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
        creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
    except Exception as e:  # noqa: BLE001
        p["tries"] += 1
        p["error"] = str(e)[-300:]
        if p["tries"] < 3 and clip and acc:
            p.update(status="scheduled", due_at=time.time() + 600)  # try again in 10 minutes
        else:
            p["status"] = "failed"
            creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
            alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
    _save()
    _mirror(p)


def scheduled_count() -> int:
    return sum(1 for p in _mine() if p.get("status") in OPEN)


def _run():
    while True:
        time.sleep(5)
        due = [p for p in list(_posts) if p["status"] == "scheduled" and p["due_at"] <= time.time() and p.get("user")]
        for p in due:
            try:
                with ctx.as_user(p["user"]):
                    if accounts.settings()["paused"]:
                        continue
                    with _lock:
                        if p["status"] != "scheduled":
                            continue
                        p["status"] = "posting"
                    _post(p)
            except Exception:  # noqa: BLE001
                pass


def _stats_for(recent):
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            stats = accounts.youtube_stats(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in stats:
                    st = stats[p["remote_id"]]
                    p["views"] = st.get("views", 0)
                    p["likes"] = st.get("likes", 0)
                    p["comments"] = st.get("comments", 0)
                    _mirror(p)
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v
                _mirror(p)


def refresh_stats():
    with _lock:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p.get("status") == "posted" and p.get("remote_id")])
            except Exception:  # noqa: BLE001
                pass
        _save()


def _stats():
    while True:
        refresh_stats()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be cancelled.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled posts can be retried.")
        p.update(status="scheduled", due_at=time.time(), tries=0, error=None)
        _save()
    _mirror(p)


def reschedule(pid: str, target_time: float):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be rescheduled.")
        p["due_at"] = round(float(target_time), 2)
        _save()
    _mirror(p)


def swap_posts(pid1: str, pid2: str):
    with _lock:
        p1 = next((p for p in _mine() if p["id"] == pid1), None)
        p2 = next((p for p in _mine() if p["id"] == pid2), None)
        if not p1 or not p2 or p1["status"] != "scheduled" or p2["status"] != "scheduled":
            raise ValueError("Both posts must be waiting scheduled posts.")
        p1["due_at"], p2["due_at"] = p2["due_at"], p1["due_at"]
        _save()
    _mirror(p1)
    _mirror(p2)


def schedule_calendar(days_ahead=21) -> dict:
    now_ist = datetime.datetime.now(IST)
    start_date = _schedule_start_date()
    mine = _mine()
    post_map = {}
    for p in mine:
        if p.get("due_at") and p.get("status") in OPEN + ("posted",):
            post_map[round(p["due_at"])] = {
                "id": p["id"],
                "title": p.get("title") or "Clip",
                "status": p.get("status"),
                "platform": p.get("platform"),
                "account_name": p.get("account_name"),
                "score": p.get("score"),
                "job": p.get("job"),
                "clip": p.get("clip"),
                "views": p.get("views"),
                "likes": p.get("likes"),
                "comments": p.get("comments"),
                "url": p.get("url"),
                "due_at": p.get("due_at"),
            }

    days = []
    for i in range(days_ahead):
        target_date = (now_ist + datetime.timedelta(days=i)).date()
        is_first_3 = _is_first_3_weeks(target_date)
        max_daily = _max_shorts_for_date(target_date)
        slots_times = _slots_for_date(target_date)
        day_slots = []
        for hour, minute in slots_times:
            dt = datetime.datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=IST)
            ts = dt.timestamp()
            matched = None
            for taken_ts, p_data in post_map.items():
                if abs(taken_ts - ts) < 600:
                    matched = p_data
                    break
            day_slots.append({
                "time": dt.strftime("%I:%M %p"),
                "time_str": dt.strftime("%I:%M %p"),
                "timestamp": round(ts, 2),
                "past": ts < time.time(),
                "post": matched,
            })

        booked_count = sum(1 for s in day_slots if s["post"])
        days.append({
            "date": target_date.strftime("%Y-%m-%d"),
            "weekday": target_date.strftime("%A"),
            "day_name": target_date.strftime("%A"),
            "short_date": target_date.strftime("%d %b"),
            "is_first_3_weeks": is_first_3,
            "phase": "First 3 Weeks (4/day)" if is_first_3 else "Standard (8/day)",
            "max_slots": max_daily,
            "slots_count": max_daily,
            "booked_slots": booked_count,
            "filled_count": booked_count,
            "free_slots": max(0, max_daily - booked_count),
            "slots": day_slots,
        })

    return {
        "start_date": start_date.strftime("%Y-%m-%d"),
        "total_scheduled": sum(1 for p in mine if p.get("status") in OPEN),
        "days": days,
        "paused": accounts.settings()["paused"],
    }


def overview() -> dict:
    today = _day(time.time())
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p["status"] == "posted" and _day(p["posted_at"]) == today),
            "posted_total": sum(1 for p in ps if p["status"] == "posted"),
            "queued": sum(1 for p in ps if p["status"] in OPEN), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    queue = sorted((p for p in _posts if p["status"] in OPEN), key=lambda p: p["due_at"])
    done = sorted((p for p in _posts if p["status"] in ("posted", "failed")), key=lambda p: -(p.get("posted_at") or p["due_at"]))[:25]
    show = [{**{k: v for k, v in p.items() if k != "user"}, "creator_name": names.get(p["creator"], "")} for p in queue + done]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if p["status"] == "posted" and _day(p["posted_at"]) in posted:
            posted[_day(p["posted_at"])] += 1
            views[_day(p["posted_at"])] += p["views"] or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]

    posted_posts = [p for p in _posts if p.get("status") == "posted"]
    total_views = sum(p.get("views") or 0 for p in posted_posts)
    total_likes = sum(p.get("likes") or 0 for p in posted_posts)
    total_comments = sum(p.get("comments") or 0 for p in posted_posts)
    top_clips = sorted(posted_posts, key=lambda p: (p.get("views") or 0, p.get("likes") or 0), reverse=True)[:6]
    engagement = round((total_likes / total_views * 100), 1) if total_views > 0 else 0.0
    analytics = {
        "views": total_views,
        "likes": total_likes,
        "comments": total_comments,
        "engagement": engagement,
        "top": [{k: p.get(k) for k in ("id", "title", "views", "likes", "comments", "url", "posted_at", "platform")} for p in top_clips],
    }

    return {"paused": accounts.settings()["paused"], "creators": rows, "posts": show, "daily": daily,
            "analytics": analytics, "alerts": alerts.configured(), "recording": len(live.active_mine())}


def digest_text() -> str:
    o = overview()
    lines = ["SnipForge daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def start():
    with _lock:
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
        _save()
    realign_scheduled_queue()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
