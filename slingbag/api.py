"""Web-API: HTTP/JSON-koppeling tussen de begeleidersapp en de slingbag-service (FR-07, TR-07).

Alle routes zijn alleen bereikbaar binnen het lokale netwerk of de eigen wifi van de slingbag.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from .config import ConfigError
from .speech import SpeechUnavailable
from .texts import DEFAULT_TEXTS

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
BUTTONS = {"groen", "geel", "blauw", "rood"}
_RECORDING_ID = re.compile(r"^[a-f0-9]{12}$")
MAX_UPLOAD = 15 * 1024 * 1024


def create_app(service) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD

    def error(message: str, status: int = 400, **extra):
        return jsonify({"fout": message, **extra}), status

    # --- app-bestanden -------------------------------------------------------------

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/<path:name>")
    def static_file(name: str):
        return send_from_directory(WEB_DIR, name)

    # --- status en instellingen -----------------------------------------------------

    @app.get("/api/status")
    def status():
        return jsonify(service.status())

    @app.get("/api/config")
    def get_config():
        return jsonify(service.config_store.get())

    @app.put("/api/config")
    def put_config():
        try:
            service.config_store.save(request.get_json(force=True))
        except ConfigError as exc:
            return error("De instellingen zijn niet opgeslagen.", 422, fouten=exc.errors)
        service.config_saved()
        return jsonify({"ok": True})

    @app.get("/api/standaardteksten")
    def default_texts():
        return jsonify(DEFAULT_TEXTS)

    @app.get("/api/stemmen")
    def voices():
        return jsonify(service.speech.engine.voices())

    # --- acties -----------------------------------------------------------------------

    @app.post("/api/acties/uitnodigen")
    def invite():
        activity_id = (request.get_json(silent=True) or {}).get("activiteit", "")
        ok, message = service.call(lambda: service.controller.invite(activity_id, "app"))
        return (jsonify({"ok": True, "melding": message}) if ok else error(message, 409))

    @app.post("/api/acties/stop")
    def stop():
        service.call(lambda: service.controller.stop("app"))
        return jsonify({"ok": True})

    @app.post("/api/acties/voorbeeld")
    def preview():
        """Laat een tekst of opname horen op de slingbag zelf, zodat de begeleider het volume kan beoordelen."""
        data = request.get_json(silent=True) or {}
        config = service.config_store.get()
        item = {"tekst": data.get("tekst", ""), "opname": data.get("opname")}
        voice = data.get("stem") or config["stem"]
        if "volume" in data:
            service.audio.set_volume(int(data["volume"]))
        try:
            path = service.speech.resolve(item, voice)
        except SpeechUnavailable as exc:
            return error(str(exc), 422)
        if service.controller.state != "rust":
            return error("Wacht tot de huidige sessie klaar is.", 409)
        service.audio.play([path])
        return jsonify({"ok": True})

    @app.post("/api/knop/<color>")
    def button(color: str):
        if color not in BUTTONS:
            return error("Onbekende knop.", 404)
        service.press(color)
        return jsonify({"ok": True})

    # --- opnames ----------------------------------------------------------------------

    @app.post("/api/opnames")
    def upload_recording():
        upload = request.files.get("bestand")
        if upload is None or not upload.filename:
            return error("Kies een audiobestand.")
        suffix = Path(upload.filename).suffix[:8] or ".audio"
        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            upload.save(tmp.name)
            try:
                result = service.speech.import_recording(Path(tmp.name))
            except SpeechUnavailable as exc:
                return error(str(exc), 422)
        return jsonify(result)

    @app.get("/api/opnames/<recording_id>")
    def get_recording(recording_id: str):
        path = service.speech.recording_path(recording_id)
        if not _RECORDING_ID.match(recording_id) or not path.exists():
            return error("Opname niet gevonden.", 404)
        return send_file(path, mimetype="audio/wav")

    # --- logboek ----------------------------------------------------------------------

    @app.get("/api/logboek")
    def logbook():
        return jsonify(service.logbook.tail(int(request.args.get("aantal", 50))))

    @app.get("/api/logboek.csv")
    def logbook_csv():
        return Response(service.logbook.read_csv(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=slingbag-logboek.csv"})

    @app.delete("/api/logboek")
    def clear_logbook():
        service.logbook.clear()
        return jsonify({"ok": True})

    # --- systeem ----------------------------------------------------------------------

    @app.post("/api/systeem/afsluiten")
    def shutdown():
        service.shutdown()
        return jsonify({"ok": True, "simulatie": service.panel.simulated})

    @app.post("/api/systeem/klok")
    def set_clock():
        epoch_ms = (request.get_json(silent=True) or {}).get("tijd_ms")
        if not isinstance(epoch_ms, (int, float)):
            return error("Geef de tijd van de telefoon mee.")
        return jsonify({"gelijkgezet": service.set_clock(epoch_ms / 1000)})

    @app.after_request
    def no_cache(response):
        # De app moet altijd de actuele status en bestanden tonen.
        response.headers["Cache-Control"] = "no-store"
        return response

    return app
