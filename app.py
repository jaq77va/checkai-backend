import os
import logging
import urllib.request
from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
from google.genai import types
from supabase import create_client, Client

app = Flask(__name__)
CORS(app)  # Permette le chiamate API dalle pagine WordPress / CheckAI

# --- CONFIGURAZIONE LOGGING ---
# Imposta il formato dei messaggi: [Data Ora] [Livello] [File:Linea] Messaggio
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s in %(module)s:%(lineno)d - %(message)s'
)
logger = app.logger

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
        "version": "2.0.0"
    })

# --- ENDPOINT 1: Upload Multiplo e Elaborazione IA Automatica ---
@app.route("/api/upload", methods=["POST"])
def upload_audio():
    try:
        token = request.form.get("token")
        audio_files = request.files.getlist("audio")

        if not token or not audio_files:
            return jsonify({"error": "Parametri mancanti (token o file audio)"}), 400

        # Verifica cliente tramite token
        client_res = supabase.table("clients").select("*").eq("token", token).execute()
        if not client_res.data:
            return jsonify({"error": "Token cliente non valido"}), 404
        
        client_data = client_res.data[0]
        coach_id = client_data["coach_id"]
        client_id = client_data["id"]

        audio_urls = []
        audio_parts = []

        # Caricamento di tutti i file audio su Supabase Storage (fino a un massimo di 5)
        for idx, audio_file in enumerate(audio_files[:5]):
            file_bytes = audio_file.read()
            file_name = f"checkin_{client_id}_{os.urandom(4).hex()}_{idx}.wav"
            
            supabase.storage.from_("audio_checkins").upload(file_name, file_bytes, {"content-type": "audio/wav"})
            audio_url = supabase.storage.from_("audio_checkins").get_public_url(file_name)
            audio_urls.append(audio_url)

            # Prepara il file audio per l'analisi multimodale di Gemini
            audio_parts.append(types.Part.from_bytes(data=file_bytes, mime_type="audio/wav"))

        # Elaborazione automatica immediata con Gemini
        ai_analysis = "Analisi non disponibile."
        reply_draft = "Bozza di risposta non disponibile."

        if client_ai and audio_parts:
            prompt = """
            Sei un assistente esperto in fisiologia e coaching per preparatori atletici e personal trainer.
            Ascolta attentamente le registrazioni vocali inviate dal cliente e genera un report dettagliato e ordinato secondo i seguenti punti:
            - Peso e Misure
            - Aderenza Dieta
            - Recupero e Sonno
            - Criticità e Dolori
            
            Fornisci risposte chiare e concise. Includi infine una bozza di risposta motivazionale pronta da inviare via chat o email al cliente.
            """

            contents = [prompt] + audio_parts
            response = client_ai.models.generate_content(
                model='gemini-3.8-flash',
                contents=contents
            )
            ai_analysis = response.text
            reply_draft = "Ciao! Ho ascoltato i tuoi aggiornamenti vocali. Ottimo lavoro questa settimana, continuiamo a spingere su questa strada."

        # Inserimento record nel DB con tutti i campi pronti per il coach
        supabase.table("checkins").insert({
            "client_id": client_id,
            "coach_id": coach_id,
            "audio_urls": audio_urls,
            "ai_analysis": ai_analysis,
            "reply_draft": reply_draft,
            "status": "processed"
        }).execute()

        return jsonify({"success": True, "message": "Check-in inviato e analizzato con successo!"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- ENDPOINT 2: Lista Check-in per il Coach (con IA già pronta) ---
@app.route("/api/coach/checkins", methods=["GET"])
def get_coach_checkins():
    coach_id = request.args.get("coach_id")
    if not coach_id:
        return jsonify({"error": "Parametro coach_id mancante"}), 400

    try:
        response = supabase.table("checkins") \
            .select("*, clients(name)") \
            .eq("coach_id", coach_id) \
            .order("created_at", desc=True) \
            .execute()
        return jsonify(response.data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- ENDPOINT 3: Lettura e Salvataggio Impostazioni Cluster Coach ---
@app.route("/api/coach/settings", methods=["GET", "POST"])
def manage_coach_settings():
    logger.info(f"Ricevuta richiesta {request.method} su /api/coach/settings")
    if request.method == "GET":
        coach_id_param = request.args.get("coach_id")
    else:
        req_data = request.get_json(silent=True) or {}
        coach_id_param = req_data.get("coach_id")
        
    logger.info(f"Parametro coach_id ricevuto: {coach_id_param}")
    
    if not coach_id_param:
        logger.warning("Richiesta respinta: parametro coach_id mancante")
        return jsonify({"error": "Parametro coach_id mancante"}), 400

    try:
        # Risolvi UUID reale del coach
        coach_res = supabase.table("coaches") \
            .select("id") \
            .eq("id", coach_id_param) \
            .execute()

        if not coach_res.data:
            logger.warning(f"Coach non trovato nel DB per ID/Code: {coach_id_param}")
            return jsonify({"error": "Coach non trovato"}), 404

        real_coach_id = coach_res.data[0]["id"]
        logger.info(f"Coach identificato con UUID: {real_coach_id}")
        
        if request.method == "GET":
            res = supabase.table("coach_clusters") \
                .select("cluster, selected") \
                .eq("coach_id", real_coach_id) \
                .execute()

            cluster_list = res.data if res.data else []
            logger.info(f"Trovati {len(cluster_list)} cluster per il coach {real_coach_id}")
            
            return jsonify({
                "coach_id": real_coach_id, 
                "clusters": res.data if res.data else []
            }), 200

        elif request.method == "POST":
            req_data = request.get_json(silent=True) or {}
            clusters_payload = req_data.get("clusters", [])

            for item in clusters_payload:
                supabase.table("coach_clusters").upsert({
                    "coach_id": real_coach_id,
                    "cluster": item["cluster"],
                    "selected": item["selected"]
                }, on_conflict="coach_id, cluster").execute()
                
            logger.info(f"Impostazioni cluster aggiornate con successo per il coach {real_coach_id}")
            return jsonify({
                "success": True, 
                "message": "Impostazioni aggiornate con successo!"
            }), 200

    except Exception as e:
        # Registra l'errore completo con tanto di stracktrace nel log di Render
        logger.error(f"Errore critico in manage_coach_settings: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# --- ENDPOINT 4: Ri-analisi Check-in con Nuovi Cluster ---
@app.route("/api/coach/reanalyze", methods=["POST"])
def reanalyze_checkin():
    req_data = request.get_json(silent=True) or {}
    checkin_id = req_data.get("checkin_id")

    if not checkin_id:
        return jsonify({"error": "Parametro checkin_id mancante"}), 400

    try:
        # 1. Recupera il checkin dal DB
        checkin_res = supabase.table("checkins").select("*").eq("id", checkin_id).execute()
        if not checkin_res.data:
            return jsonify({"error": "Check-in non trovato"}), 404

        checkin = checkin_res.data[0]
        coach_id = checkin["coach_id"]
        audio_urls = checkin.get("audio_urls") or ([checkin.get("audio_url")] if checkin.get("audio_url") else [])

        if not audio_urls:
            return jsonify({"error": "Nessun file audio associato a questo check-in"}), 400

        # 2. Recupera i file audio da Supabase Storage per Gemini
        audio_parts = []
        for url in audio_urls:
            # Estrae il nome del file dall'URL di Supabase
            file_name = url.split("/")[-1]
            audio_bytes = supabase.storage.from_("audio_checkins").download(file_name)
            audio_parts.append(types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"))

        # 3. Leggi i cluster attivi aggiornati del coach
        c_res = supabase.table("coach_clusters") \
            .select("cluster") \
            .eq("coach_id", coach_id) \
            .eq("selected", True) \
            .execute()
        
        clusters = [row["cluster"] for row in c_res.data] if c_res.data else ["Peso e Misure", "Aderenza Dieta", "Recupero e Sonno", "Criticità e Dolori"]
        clusters_text = "\n".join([f"- {c}" for c in clusters])

        # 4. Richiesta a Gemini con il nuovo prompt
        prompt = f"""
        Sei un assistente esperto in fisiologia e coaching per preparatori atletici e personal trainer.
        Ascolta attentamente le registrazioni vocali inviate dal cliente e genera un report dettagliato e ordinato secondo i seguenti punti richiesti dal coach:

        {clusters_text}

        Fornisci risposte chiare e concise per ciascun punto. Includi infine una bozza di risposta motivazionale pronta da inviare via chat o email al cliente.
        """

        contents = [prompt] + audio_parts
        response = client_ai.models.generate_content(
            model='gemini-3.8-flash',
            contents=contents
        )
        new_analysis = response.text
        new_reply = "Ciao! Ho rianalizzato i tuoi aggiornamenti vocali con i nuovi parametri. Continuiamo a spingere su questa strada!"

        # 5. Aggiorna il record nel DB
        supabase.table("checkins").update({
            "ai_analysis": new_analysis,
            "reply_draft": new_reply
        }).eq("id", checkin_id).execute()

        logger.info(f"Check-in {checkin_id} rianalizzato con successo!")
        return jsonify({
            "success": True, 
            "ai_analysis": new_analysis, 
            "reply_draft": new_reply
        }), 200

    except Exception as e:
        logger.error(f"Errore ri-analisi: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500
        
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
