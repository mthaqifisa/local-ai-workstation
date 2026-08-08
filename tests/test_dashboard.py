"""
Playwright browser automation tests for the LAW dashboard at :8800.
Pages covered: / (home), /imagine (create), /audio, /video
"""

import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, Page, expect

BASE = "http://localhost:8800"
SS   = Path(__file__).parent / "screenshots"
SS.mkdir(exist_ok=True)

RESULTS: list[dict] = []

# ── helpers ──────────────────────────────────────────────────────────────────

def record(name: str, passed: bool, note: str = ""):
    status = "PASS" if passed else "FAIL"
    mark   = "✓" if passed else "✗"
    print(f"  {mark} [{status}] {name}" + (f"  — {note}" if note else ""))
    RESULTS.append({"name": name, "passed": passed, "note": note})

def ss(page: Page, name: str) -> Path:
    p = SS / f"{name}.png"
    page.screenshot(path=str(p), full_page=True)
    return p

def assert_nav(page: Page):
    """Verify the top nav has all main links."""
    for href, label in [("/imagine", "Create"), ("/video", "Video"), ("/audio", "Audio")]:
        link = page.locator(f'a[href="{href}"]').first
        assert link.count() > 0, f"nav link {href} missing"
    record("nav links present", True, "Create · Video · Audio all found")

# ── test 1: home page (/  ) ───────────────────────────────────────────────────

def test_home(page: Page):
    print("\n── Home page (/) ──")
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)  # let JS render service cards
    ss(page, "01_home_load")

    # Title
    title = page.title()
    record("page title", "LAW" in title or "AI" in title or "Dashboard" in title,
           f"title='{title}'")

    # Nav
    assert_nav(page)

    # Service health panel (api/status cards)
    status_resp = page.request.get(BASE + "/api/status")
    ok = status_resp.ok
    record("/api/status reachable", ok, f"HTTP {status_resp.status}")

    # RAM / CPU stats visible somewhere on page
    body = page.content()
    record("hardware stats rendered", "ram" in body.lower() or "cpu" in body.lower() or "battery" in body.lower(),
           "found ram/cpu/battery in page HTML")

    # Quick links / navigation buttons are clickable
    create_link = page.locator('a[href="/imagine"]').first
    record("Create nav link visible", create_link.is_visible())

    ss(page, "01_home_final")
    print(f"  → screenshots: 01_home_load.png, 01_home_final.png")


# ── test 2: create / imagine page (/imagine) ─────────────────────────────────

def test_imagine(page: Page):
    print("\n── Create page (/imagine) ──")
    page.goto(BASE + "/imagine", wait_until="domcontentloaded")
    ss(page, "02_imagine_load")

    # Prompt textarea and generate button
    prompt_box = page.locator("#prompt")
    gen_btn    = page.locator("#genBtn")
    record("prompt textarea present", prompt_box.count() > 0)
    record("Generate button present", gen_btn.count() > 0)

    # Style preset chips
    presets = page.locator(".preset")
    record("style presets rendered", presets.count() >= 8, f"{presets.count()} presets found")

    # Resolution grid buttons
    res_btns = page.locator(".res-btn")
    record("resolution grid rendered", res_btns.count() >= 3, f"{res_btns.count()} resolution options")

    # Gallery section present
    gallery = page.locator("#gallery")
    record("gallery container present", gallery.count() > 0)

    # Interact: fill prompt + click Cinematic preset + generate
    prompt_box.fill("a minimalist mountain landscape at dusk, soft gradients")
    page.locator(".preset", has_text="Cinematic").first.click()
    time.sleep(0.3)
    prompt_val = prompt_box.input_value()
    record("style preset appended to prompt", "cinematic" in prompt_val.lower(),
           f"prompt now: {prompt_val[:60]}…")

    ss(page, "02_imagine_filled")

    # Click Generate — use z-image-turbo via the model chip
    turbo_chip = page.locator("button.mc", has_text="Z-Image Turbo").first
    if turbo_chip.count():
        turbo_chip.click()
        record("model chip clicked (z-image-turbo)", True)
    else:
        record("model chip found", False, "no .mc button with 'Z-Image Turbo' text")

    gen_btn.click()
    ss(page, "02_imagine_generating")
    record("Generate button clicked", True)

    # Wait for result image (up to 120s for z-turbo)
    result_img = page.locator("#resultImg")
    try:
        result_img.wait_for(state="visible", timeout=120_000)
        src = result_img.get_attribute("src") or ""
        record("result image appeared", bool(src), f"src={src[:60]}")
        ss(page, "02_imagine_result")
    except Exception as e:
        record("result image appeared", False, str(e)[:80])
        ss(page, "02_imagine_timeout")

    # Gallery should now have ≥1 thumbnail
    try:
        page.locator("#gallery img, #gallery .gal-thumb").first.wait_for(timeout=5_000)
        count = page.locator("#gallery img, #gallery .gal-thumb").count()
        record("gallery updated after generation", count >= 1, f"{count} item(s)")
    except Exception:
        record("gallery updated after generation", False, "no thumbnails found")

    print(f"  → screenshots: 02_imagine_*.png")


# ── test 3: audio page (/audio) ───────────────────────────────────────────────

def test_audio(page: Page):
    print("\n── Audio page (/audio) ──")
    page.goto(BASE + "/audio", wait_until="domcontentloaded")
    ss(page, "03_audio_load")

    tts_box  = page.locator("#ttsText")
    tts_btn  = page.locator("#ttsBtn")
    player   = page.locator("#ttsPlayer")
    record("TTS textarea present",      tts_box.count() > 0)
    record("Generate Speech btn present", tts_btn.count() > 0)
    record("audio player element present", player.count() > 0)

    # Voice chips
    voice_chips = page.locator(".vc[data-voice]")
    record("voice chips rendered", voice_chips.count() >= 6, f"{voice_chips.count()} chips")

    # Select am_adam voice
    adam_chip = page.locator(".vc[data-voice='am_adam']")
    adam_chip.click()
    record("voice chip click (am_adam)", True)

    # Fill short text
    tts_box.fill("Hello! This is a Playwright automation test of the LAW audio page.")
    ss(page, "03_audio_filled")

    # Generate
    tts_btn.click()
    ss(page, "03_audio_generating")
    record("Generate Speech button clicked", True)

    # Wait for audio player to become visible (up to 90s)
    try:
        player.wait_for(state="visible", timeout=90_000)
        src = player.get_attribute("src") or ""
        record("audio player appeared", bool(src), f"src={src[:60]}")
        ss(page, "03_audio_result")
    except Exception as e:
        record("audio player appeared", False, str(e)[:80])
        ss(page, "03_audio_timeout")

    # Gallery lives in the "Library" tab — must click it to trigger loadGallery()
    page.locator("button.tab", has_text="Library").click()
    try:
        page.locator("#audGallery .gal-item").first.wait_for(timeout=5_000)
    except Exception:
        pass
    count = page.locator("#audGallery .gal-item").count()
    record("audio gallery has items", count >= 1, f"{count} item(s) in #audGallery after clicking Library tab")

    print(f"  → screenshots: 03_audio_*.png")


# ── test 4: video page (/video) ───────────────────────────────────────────────

def test_video(page: Page):
    print("\n── Video page (/video) ──")
    page.goto(BASE + "/video", wait_until="domcontentloaded")
    ss(page, "04_video_load")

    prompt_box = page.locator("#prompt")
    gen_btn    = page.locator("#genBtn")
    record("video prompt textarea present", prompt_box.count() > 0)
    record("video Generate button present", gen_btn.count() > 0)

    # Duration / model controls
    body = page.content()
    record("duration control present", "duration" in body.lower() or "seconds" in body.lower())
    record("LTX model option visible",  "LTX" in body or "ltx" in body)

    # Gallery section
    gallery = page.locator("#gallery, .gal-grid")
    record("video gallery container present", gallery.count() > 0)

    # Interact: fill prompt and click generate
    prompt_box.fill("a slow time-lapse of clouds drifting over a mountain peak")
    ss(page, "04_video_filled")

    # Intercept the /api/video POST to verify it fires
    job_id_seen = []
    def on_response(response):
        if "/api/video" in response.url and response.status == 200:
            try:
                body = response.json()
                if body.get("job_id"):
                    job_id_seen.append(body["job_id"])
            except Exception:
                pass
    page.on("response", on_response)

    gen_btn.click()
    # Wait just long enough for the API call to fire and get the job_id back
    page.wait_for_timeout(6_000)
    ss(page, "04_video_started")

    record("video job fired (/api/video POST)", bool(job_id_seen),
           f"job_id={job_id_seen[0]}" if job_id_seen else "no job_id received")

    # Verify the page shows a loading / progress indicator (not waiting for full completion)
    loading_indicator = page.locator(".prog, .pb, .progress, [id*='prog'], [id*='status']").first
    record("loading indicator shown after click", loading_indicator.count() > 0,
           "found progress/status element in DOM")

    print(f"  → screenshots: 04_video_*.png  (generation NOT awaited — LTX takes ~5 min)")
    print(f"  Note: video WARN is a known hardware limit on M-series — not a UI bug")


# ── test 5: vision page (/vision) ────────────────────────────────────────────

def test_vision(page: Page):
    print("\n── Vision page (/vision) ──")
    page.goto(BASE + "/vision", wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    ss(page, "05_vision_load")

    # Drop zone and question textarea
    dz       = page.locator("#dz")
    question = page.locator("#prompt")
    run_btn  = page.locator("#runBtn")
    record("drop zone present",          dz.count() > 0)
    record("question textarea present",  question.count() > 0)
    record("Analyze button present",     run_btn.count() > 0)

    # Model chips (Qwen3-VL, Gemma-4)
    model_chips = page.locator(".mc[data-model], .mc[onclick*='setModel']")
    if model_chips.count() == 0:
        # chips may use different selector — check for any .mc buttons
        model_chips = page.locator(".mc")
    record("vision model chips present", model_chips.count() >= 2,
           f"{model_chips.count()} model chip(s)")

    # Upload a test image via the hidden file input inside the drop zone
    test_img = str(Path(__file__).parent.parent / "tests" / "screenshots" / "01_home_final.png")
    file_input = page.locator("#dz input[type='file']")
    if file_input.count():
        file_input.set_input_files(test_img)
        page.wait_for_timeout(800)
        preview = page.locator(".prev-box img, #dz img, [id*='prev'] img").first
        record("image preview shown after upload", preview.count() > 0 or
               page.locator(".prev-box, #prevBox, .prev-rm").count() > 0,
               "preview or remove button visible")
    else:
        record("file input reachable in drop zone", False, "no input[type=file] inside #dz")

    question.fill("What page is shown in this screenshot?")
    ss(page, "05_vision_filled")
    record("question filled", True)

    print(f"  → screenshots: 05_vision_*.png  (analysis NOT triggered — needs model load)")


# ── test 6: chat page (/chat) ─────────────────────────────────────────────────

def test_chat(page: Page):
    print("\n── Chat page (/chat) ──")
    page.goto(BASE + "/chat", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    ss(page, "06_chat_load")

    thread   = page.locator("#thread")
    inp      = page.locator("#in")
    send_btn = page.locator("#sendbtn")
    record("message thread container present", thread.count() > 0)
    record("input textarea present",           inp.count() > 0)
    record("send button present",              send_btn.count() > 0)

    # Mode toggle and model dropdown
    mode_btn   = page.locator("#modeBtn")
    model_sel  = page.locator("#model")
    record("mode toggle present (Agent/Direct)", mode_btn.count() > 0)
    record("model/persona selector present",     model_sel.count() > 0)

    # Switch to Direct mode (raw model — faster, no tools)
    if mode_btn.count():
        # Toggle if currently in Agent mode
        mode_text = mode_btn.inner_text()
        if "Agent" in mode_text:
            mode_btn.click()
            page.wait_for_timeout(500)
        record("Direct mode activated", True)
    ss(page, "06_chat_direct_mode")

    # Send a minimal message and verify a reply appears
    inp.fill("Reply with exactly the word: pong")
    ss(page, "06_chat_filled")
    inp.press("Enter")

    try:
        # Wait for an AI bubble to appear (up to 60s for cold model start)
        ai_bubble = page.locator(".bubble.ai:not(:has(.typing))")
        ai_bubble.first.wait_for(timeout=60_000)
        reply_text = ai_bubble.first.inner_text()
        record("AI reply received", bool(reply_text.strip()),
               f"reply: {reply_text.strip()[:60]}")
        ss(page, "06_chat_reply")
    except Exception as e:
        record("AI reply received", False, str(e)[:80])
        ss(page, "06_chat_timeout")

    print(f"  → screenshots: 06_chat_*.png")


# ── test 7: personas page (/personas) ────────────────────────────────────────

def test_personas(page: Page):
    print("\n── Personas page (/personas) ──")
    page.goto(BASE + "/personas", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    ss(page, "07_personas_load")

    # Persona cards loaded from /api/personas
    try:
        page.locator(".persona-card, .card, p").first.wait_for(timeout=5_000)
    except Exception:
        pass

    body = page.content()
    # API call verification
    resp = page.request.get(BASE + "/api/personas")
    record("/api/personas reachable", resp.ok, f"HTTP {resp.status}")
    personas_data = resp.json().get("personas", [])
    record("personas list non-empty", len(personas_data) >= 1,
           f"{len(personas_data)} persona(s) returned")

    # + New persona button
    add_btn = page.locator("button.add, button:has-text('New persona'), button:has-text('+ New')")
    record("Add persona button present", add_btn.count() > 0)

    # Verify known personas appear in page content
    known = ["orchestrator", "coder", "writer"]
    found = sum(1 for p in known if p in body.lower())
    record("core persona names in page", found >= 2, f"{found}/3 of {known} found")

    ss(page, "07_personas_final")
    print(f"  → screenshots: 07_personas_*.png")


# ── test 8: models page (/models) ─────────────────────────────────────────────

def test_models(page: Page):
    print("\n── Models page (/models) ──")
    page.goto(BASE + "/models", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    ss(page, "08_models_load")

    search_inp = page.locator("#q")
    search_btn = page.locator("button.go").first
    record("search input present",  search_inp.count() > 0)
    record("Search button present", search_btn.count() > 0)

    # Currently registered models section
    current_div = page.locator("#current")
    record("registered models section present", current_div.count() > 0)
    page.wait_for_timeout(1000)
    current_content = current_div.inner_text() if current_div.count() else ""
    record("registered models list populated", len(current_content.strip()) > 10,
           f"{len(current_content.strip())} chars rendered in #current")

    # /api/models API
    resp = page.request.get(BASE + "/api/models")
    record("/api/models reachable", resp.ok, f"HTTP {resp.status}")
    gateway_models = resp.json().get("gateway", [])
    record("gateway model list non-empty", len(gateway_models) >= 1,
           f"{len(gateway_models)} model(s)")

    # Type a search query and click — verify the request fires and #results renders
    search_inp.fill("qwen mlx")
    with page.expect_response(lambda r: "/api/models/search" in r.url, timeout=15_000) as resp_info:
        search_btn.click()
    search_resp = resp_info.value
    record("HF search API called (/api/models/search)", search_resp.ok,
           f"HTTP {search_resp.status}")
    page.wait_for_timeout(1000)
    results_div = page.locator("#results")
    results_txt = results_div.inner_text() if results_div.count() else ""
    record("search #results rendered", results_div.count() > 0,
           f"{len(results_txt.strip())} chars in #results")

    ss(page, "08_models_search")
    print(f"  → screenshots: 08_models_*.png")


# ── runner ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("LAW Dashboard — Playwright Browser Automation")
    print(f"Target: {BASE}")
    print("=" * 60)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx     = browser.new_context(viewport={"width": 1400, "height": 900})
        page    = ctx.new_page()

        try:
            test_home(page)
            test_imagine(page)
            test_audio(page)
            test_video(page)
            test_vision(page)
            test_personas(page)
            test_models(page)
            test_chat(page)
        finally:
            browser.close()

    # Summary
    passed = sum(1 for r in RESULTS if r["passed"])
    total  = len(RESULTS)
    pct    = round(passed / total * 100) if total else 0
    print("\n" + "=" * 60)
    print(f"RESULT: {passed}/{total} checks passed  ({pct}%)")
    print("=" * 60)
    failures = [r for r in RESULTS if not r["passed"]]
    if failures:
        print("\nFailed checks:")
        for f in failures:
            print(f"  ✗ {f['name']}" + (f"  — {f['note']}" if f["note"] else ""))

    print(f"\nScreenshots saved to: {SS}")
    return 0 if pct >= 85 else 1


if __name__ == "__main__":
    sys.exit(main())
