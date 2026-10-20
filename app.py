import os
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
from google.genai import types
from supabase import create_client, Client

app = Flask(__name__)
CORS(app)  # Permette le chiamate API dalle pagine WordPress / CheckAI

# --- CONFIGURAZIONE LOGGING ---
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
        "version": "2.2.0"
    })

# --- FUNZIONE DI SUPPORTO CON LOGGING DEI CLUSTER ---
def get_coach_prompt(coach_id):
    clusters = []
    logger.info(f"Ricerca cluster attivi a DB per il coach_id: {coach_id}")
    
    try:
        # Recupera solo i cluster con selected = True per questo coach
        c_res = supabase.table("coach_clusters") \
            .select("cluster") \
            .eq("coach_id", coach_id) \
            .eq("selected", True) \
            .execute()
        
        if c_res.data:
            clusters = [row["cluster"] for row in c_res.data]
            logger.info(f"Cluster attivi trovati per il coach {coach_id}: {clusters}")
        else:
            logger.warning(f"Nessun cluster attivo trovato per il coach {coach_id}. Verrà usato il fallback.")
            
    except Exception as e:
        logger.error(f"Errore durante il recupero dei cluster per il coach {coach_id}: {str(e)}", exc_info=True)

    # Fallback predefinito se nessun cluster è selezionato nel DB
    if not clusters:
        clusters = ["Peso e Misure", "Aderenza Dieta", "Recupero e Sonno", "Criticità e Dolori"]
        logger.info(f"Utilizzo cluster di fallback predefiniti: {clusters}")

    clusters_text = "\n".join([f"- {c}" for c in clusters])

    prompt = f"""
    Sei un assistente esperto in fisiologia e coaching per preparatori atletici e personal trainer.
    Ascolta attentamente le registrazioni vocali inviate dal cliente e genera un report dettagliato e ordinato secondo i seguenti punti richiesti dal coach:

    {clusters_text}
    
    Fornisci risposte chiare e concise per ciascun punto. Includi infine una bozza di risposta motivazionale pronta da inviare via chat o email al cliente.
    """
    
    logger.info(f"Prompt IA generato con successo per il coach {coach_id} con {len(clusters)} cluster attivi.")
    return prompt

# --- ENDPOINT 1: Upload Multiplo e Elaborazione IA Automatica ---
@app.route("/api/upload", methods=["POST"])
def upload_audio():
    logger.info("Ricevuta richiesta POST su /api/upload")
    try:
        token = request.form.get("token")
        audio_files = request.files.getlist("audio")

        if not token or not audio_files:
            logger.warning("Upload rifiutato: token o file audio mancanti.")
            return jsonify({"error": "Parametri mancanti (token o file audio)"}), 400

        # Verifica cliente tramite token
        client_res = supabase.table("clients").select("*").eq("token", token).execute()
        if not client_res.data:
            logger.warning(f"Token cliente non valido: {token}")
            return jsonify({"error": "Token cliente non valido"}), 404
        
        client_data = client_res.data[0]
        coach_id = client_data["coach_id"]
        client_id = client_data["id"]
        logger.info(f"Cliente identificato: ID {client_id} associato al coach ID {coach_id}")

        audio_urls = []
        audio_parts = []

        # Caricamento di tutti i file audio su Supabase Storage (fino a 5)
        for idx, audio_file in enumerate(audio_files[:5]):
            file_bytes = audio_file.read()
            file_name = f"checkin_{client_id}_{os.urandom(4).hex()}_{idx}.wav"
            
            supabase.storage.from_("audio_checkins").upload(file_name, file_bytes, {"content-type": "audio/wav"})
            audio_url = supabase.storage.from_("audio_checkins").get_public_url(file_name)
            audio_urls.append(audio_url)

            audio_parts.append(types.Part.from_bytes(data=file_bytes, mime_type="audio/wav"))

        logger.info(f"Caricati {len(audio_parts)} file audio su Supabase Storage per il check-in.")

        # Genera il prompt dinamico con i cluster del coach
        prompt = get_coach_prompt(coach_id)

        ai_analysis = "Analisi non disponibile."
        reply_draft = "Bozza di risposta non disponibile."

        if client_ai and audio_parts:
            logger.info("Invio dei dati audio e del prompt dinamico a Gemini...")
            contents = [prompt] + audio_parts
            response = client_ai.models.generate_content(
                model='gemini-3.8-flash',
                contents=contents
            )
            ai_analysis = response.text
            reply_draft = "Ciao! Ho ascoltato i tuoi aggiornamenti vocali. Ottimo lavoro questa settimana, continuiamo a spingere su questa strada."
            logger.info("Analisi IA completata con successo tramite Gemini.")

        # Inserimento record nel DB
        supabase.table("checkins").insert({
            "client_id": client_id,
            "coach_id": coach_id,
            "audio_urls": audio_urls,
            "ai_analysis": ai_analysis,
            "reply_draft": reply_draft,
            "status": "processed"
        }).execute()

        logger.info("Check-in salvato correttamente nel database.")
        return jsonify({"success": True, "message": "Check-in inviato e analizzato con successo!"})

    except Exception as e:
        logger.error(f"Errore critico durante l'upload: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# --- ENDPOINT 2: Lista Check-in per il Coach ---
@app.route("/api/coach/checkins", methods=["GET"])
def get_coach_checkins():
    coach_id = request.args.get("coach_id")
    logger.info(f"Richiesta get_coach_checkins per coach_id: {coach_id}")
    if not coach_id:
        return jsonify({"error": "Parametro coach_id mancante"}), 400

    try:
        response = supabase.table("checkins") \
            .select("*, clients(name)") \
            .eq("coach_id", coach_id) \
            .order("created_at", desc=True) \
            .execute()
        
        logger.info(f"Trovati {len(response.data if response.data else [])} check-in per il coach {coach_id}")
        return jsonify(response.data), 200
    except Exception as e:
        logger.error(f"Errore get_coach_checkins: {str(e)}", exc_info=True)
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
        
    if not coach_id_param:
        return jsonify({"error": "Parametro coach_id mancante"}), 400

    try:
        coach_res = supabase.table("coaches") \
            .select("id") \
            .eq("id", coach_id_param) \
            .execute()

        if not coach_res.data:
            logger.warning(f"Coach non trovato nel DB per ID: {coach_id_param}")
            return jsonify({"error": "Coach non trovato"}), 404

        real_coach_id = coach_res.data[0]["id"]
        
        if request.method == "GET":
            # 1. Recupera tutti i cluster (per popolare le checkbox)
            res = supabase.table("coach_clusters") \
                .select("cluster, selected") \
                .eq("coach_id", real_coach_id) \
                .execute()

            cluster_list = res.data if res.data else []

            # 2. FILTRA E LOGGA SUBITO QUALI SONO A TRUE (ATTIVI)
            active_clusters = [item["cluster"] for item in cluster_list if item.get("selected") == True]
            logger.info(f"==> [LOGIN COACH] Il coach {real_coach_id} si è loggato. Cluster ATTIVI (true): {active_clusters} (Totale attivi: {len(active_clusters)})")
           
            return jsonify({
                "coach_id": real_coach_id, 
                "clusters": cluster_list
            }), 200

        elif request.method == "POST":
            req_data = request.get_json(silent=True) or {}
            clusters_payload = req_data.get("clusters", [])
            logger.info(f"Salvataggio impostazioni cluster per il coach {real_coach_id}. Dati in arrivo: {clusters_payload}")

            for item in clusters_payload:
                supabase.table("coach_clusters").upsert({
                    "coach_id": real_coach_id,
                    "cluster": item["cluster"],
                    "selected": item["selected"]
                }, on_conflict="coach_id, cluster").execute()
                
            logger.info(f"Cluster aggiornati con successo per il coach {real_coach_id}")
            return jsonify({
                "success": True, 
                "message": "Impostazioni aggiornate con successo!"
            }), 200

    except Exception as e:
        logger.error(f"Errore in manage_coach_settings: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# --- ENDPOINT 4: Ri-analisi Check-in Esistente con Nuovi Cluster ---
@app.route("/api/coach/reanalyze", methods=["POST"])
def reanalyze_checkin():
    req_data = request.get_json(silent=True) or {}
    checkin_id = req_data.get("checkin_id")
    logger.info(f"Ricevuta richiesta di ri-analisi IA per il checkin_id: {checkin_id}")

    if not checkin_id:
        return jsonify({"error": "Parametro checkin_id mancante"}), 400

    try:
        checkin_res = supabase.table("checkins").select("*").eq("id", checkin_id).execute()
        if not checkin_res.data:
            logger.warning(f"Check-in {checkin_id} non trovato per la ri-analisi.")
            return jsonify({"error": "Check-in non trovato"}), 404

        checkin = checkin_res.data[0]
        coach_id = checkin["coach_id"]
        audio_urls = checkin.get("audio_urls") or ([checkin.get("audio_url")] if checkin.get("audio_url") else [])

        if not audio_urls:
            logger.warning(f"Nessun file audio trovato per il check-in {checkin_id}")
            return jsonify({"error": "Nessun file audio associato a questo check-in"}), 400

        # Scarica i file audio da Supabase Storage
        audio_parts = []
        for url in audio_urls:
            file_name = url.split("/")[-1]
            audio_bytes = supabase.storage.from_("audio_checkins").download(file_name)
            audio_parts.append(types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"))

        logger.info(f"Scaricati {len(audio_parts)} file audio per la ri-analisi del check-in {checkin_id}")

        # Recupera il prompt aggiornato con i nuovi cluster attivi salvati a DB
        prompt = get_coach_prompt(coach_id)

        if client_ai and audio_parts:
            logger.info("Invio nuova richiesta a Gemini con i cluster aggiornati...")
            contents = [prompt] + audio_parts
            response = client_ai.models.generate_content(
                model='gemini-3.8-flash',
                contents=contents
            )
            new_analysis = response.text
            new_reply = "Ciao! Ho rianalizzato i tuoi aggiornamenti vocali in base ai nuovi parametri. Continuiamo così!"

            supabase.table("checkins").update({
                "ai_analysis": new_analysis,
                "reply_draft": new_reply
            }).eq("id", checkin_id).execute()

            logger.info(f"Check-in {checkin_id} rianalizzato e aggiornato con successo nel DB.")
            return jsonify({
                "success": True, 
                "ai_analysis": new_analysis, 
                "reply_draft": new_reply
            }), 200

        return jsonify({"error": "Impossibile eseguire l'analisi IA"}), 500

    except Exception as e:
        logger.error(f"Errore critico durante la ri-analisi: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
