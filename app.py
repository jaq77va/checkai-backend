import os
import urllib.request
from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
from google.genai import types
from supabase import create_client, Client

app = Flask(__name__)
CORS(app)  # Permette le chiamate API dalle pagine WordPress / CheckAI

# Inizializzazione Client API via Variabili d'Ambiente
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

client_ai = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if (SUPABASE_URL and SUPABASE_KEY) else None

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "online",
        "service": "SportDataSense CheckAI API Engine",
        "version": "1.0.0"
    })

# --- ENDPOINT 1: Caricamento Audio del Cliente ---
@app.route("/api/upload", methods=["POST"])
def upload_audio():
    try:
        token = request.form.get("token")
        audio_file = request.files.get("audio")

        if not token or not audio_file:
            return jsonify({"error": "Parametri mancanti (token o file audio)"}), 400

        # Verifica cliente tramite token
        client_res = supabase.table("clients").select("*").eq("token", token).execute()
        if not client_res.data:
            return jsonify({"error": "Token cliente non valido"}), 404
        
        client_data = client_res.data[0]
        coach_id = client_data["coach_id"]
        client_id = client_data["id"]

        # Caricamento file nello storage di Supabase
        file_bytes = audio_file.read()
        file_name = f"checkin_{client_id}_{os.urandom(4).hex()}.wav"
        
        supabase.storage.from_("audio_checkins").upload(file_name, file_bytes, {"content-type": "audio/wav"})
        audio_url = supabase.storage.from_("audio_checkins").get_public_url(file_name)

        # Inserimento record nel DB
        supabase.table("checkins").insert({
            "client_id": client_id,
            "coach_id": coach_id,
            "audio_url": audio_url,
            "status": "pending"
        }).execute()

        return jsonify({"success": True, "message": "Check-in registrato con successo!"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- ENDPOINT 2: Lista Check-in in sospeso per il Coach ---
@app.route("/api/coach/checkins", methods=["GET"])
def get_coach_checkins():
    coach_id = request.args.get("coach_id")
    if not coach_id:
        return jsonify({"error": "Parametro coach_id mancante"}), 400

    try:
        response = supabase.table("checkins").select("*, clients(name)").eq("coach_id", coach_id).eq("status", "pending").execute()
        return jsonify(response.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- ENDPOINT 3: Elaborazione Report & Bozza con Gemini ---
@app.route("/api/coach/process", methods=["POST"])
def process_checkin():
    data = request.json or {}
    checkin_id = data.get("checkin_id")
    audio_url = data.get("audio_url")
    clusters = data.get("clusters", [
        "Peso e Misure", 
        "Aderenza Dieta", 
        "Recupero e Sonno", 
        "Criticità e Dolori", 
        "Bozza Risposta"
    ])

    if not checkin_id or not audio_url:
        return jsonify({"error": "Parametri checkin_id o audio_url mancanti"}), 400

    try:
        # Scarica la traccia in locale temporaneo
        temp_audio_path = "/tmp/temp_checkin.wav"
        urllib.request.urlretrieve(audio_url, temp_audio_path)

        with open(temp_audio_path, "rb") as f:
            audio_bytes = f.read()

        clusters_text = "\n".join([f"- **{c}**" for c in clusters])

        prompt = f"""
        Sei un assistente esperto in fisiologia e coaching per preparatori atletici e personal trainer.
        Ascolta attentamente il vocale del cliente e genera un report dettagliato e ordinato secondo i seguenti punti richiesti:
        
        {clusters_text}

        Fornisci risposte chiare, concise e sintetiche. Includi infine una bozza di risposta motivazionale pronta da inviare via chat o email al cliente.
        """

        # Chiamata al modello Gemini 3.8 Flash
        response = client_ai.models.generate_content(
            model='gemini-3.8-flash',
            contents=[
                prompt,
                types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")
            ]
        )

        report_text = response.text

        # Aggiorna lo stato nel DB
        supabase.table("checkins").update({
            "status": "processed",
            "report": report_text
        }).eq("id", checkin_id).execute()

        return jsonify({"success": True, "report": report_text})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
