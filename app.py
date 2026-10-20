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
    logger.info(f"==> [AI PROMPT] Inizio recupero cluster ATTIVI (selected=true) per coach_id: {coach_id}")
    
    try:
        c_res = supabase.table("coach_clusters") \
            .select("cluster") \
            .eq("coach_id", coach_id) \
            .eq("selected", True) \
            .execute()
        
        if c_res.data:
            clusters = [row["cluster"] for row in c_res.data]
            logger.info(f"==> [AI PROMPT] Cluster ATTIVI trovati a DB: {clusters}")
        else:
            logger.warning(f"==> [AI PROMPT] Nessun cluster attivo con selected=true per il coach {coach_id}.")
            
    except Exception as e:
        logger.error(f"==> [AI PROMPT] Errore recupero cluster: {str(e)}", exc_info=True)

    if not clusters:
        logger.info(f"==> [AI PROMPT] NOT CLUSTER ALLORA SCRIVO I 4 FISSI")
        clusters = ["Peso e Misure", "Aderenza Dieta", "Recupero e Sonno", "Criticità e Dolori"]

    clusters_text = "\n".join([f"- {c}" for c in clusters])

    # PROMPT RESTRITTIVO TASSATIVO
    prompt = f"""
    Sei un assistente esperto in fisiologia e coaching per preparatori atletici e personal trainer.
    
    REGOLE TASSATIVE DI FORMATTAZIONE ED ESECUZIONE:
    1. Analizza l'audio del cliente valutando ESCLUSIVAMENTE ed UNICAMENTE i seguenti punti richiesti dal coach:
    {clusters_text}
    
    2. DIVIETO ASSOLUTO: NON aggiungere altri argomenti, categorie, sezioni o macro-aree non presenti nell'elenco sopra. Ignora completamente qualsiasi altro tema trattato dal cliente se non rientra nei punti sopra indicati.
    3. Se nell'audio non ci sono informazioni relative a uno dei punti richiesti, scrivi semplicemente: "*Dato non pervenuto*".
    4. Concludi INFINE separando la risposta con la riga esatta '---BOZZA_CLIENTE---' seguita da una breve bozza di risposta motivazionale diretta al cliente da inviare via chat/email.
    """
    
    logger.info(f"==> [AI PROMPT] Prompt finale inviato a Gemini:\n{prompt}")
    return prompt

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
            
            full_text = response.text or ""

            # Separazione dell'analisi tecnica dalla bozza per il cliente
            if "---BOZZA_CLIENTE---" in full_text:
                parts = full_text.split("---BOZZA_CLIENTE---")
                ai_analysis = parts[0].strip()
                reply_draft = parts[1].strip()
            else:
                ai_analysis = full_text.strip()
                reply_draft = "Ciao! Ho ascoltato i tuoi aggiornamenti vocali. Ottimo lavoro questa settimana, continuiamo a spingere su questa strada!"

            logger.info("Analisi IA completata e separata con successo tramite Gemini.")

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

# --- ENDPOINT 4: Ri-analisi di TUTTI i Check-in del Coach con i soli Cluster Attivi ---
@app.route("/api/coach/reanalyze", methods=["POST"])
def reanalyze_checkin():
    req_data = request.get_json(silent=True) or {}
    coach_id = req_data.get("coach_id")
    logger.info(f"==> [REANALYZE ALL] Ricevuta richiesta di ri-analisi globale per coach_id: {coach_id}")

    if not coach_id:
        return jsonify({"error": "Parametro coach_id mancante"}), 400

    try:
        # 1. Recupera TUTTI i check-in del coach
        checkins_res = supabase.table("checkins").select("*").eq("coach_id", coach_id).execute()
        if not checkins_res.data:
            return jsonify({"error": "Nessun check-in trovato per questo coach"}), 404

        checkins_list = checkins_res.data
        logger.info(f"==> [REANALYZE ALL] Trovati {len(checkins_list)} check-in da ri-analizzare per il coach {coach_id}.")

        # 2. Recupera il prompt con le REGOLE TASSATIVE sui soli cluster attivi (selected=true)
        prompt = get_coach_prompt(coach_id)

        processed_count = 0

        # 3. Cicla su ciascun check-in e ri-elabora con Gemini
        for checkin in checkins_list:
            checkin_id = checkin["id"]
            audio_urls = checkin.get("audio_urls") or ([checkin.get("audio_url")] if checkin.get("audio_url") else [])

            if not audio_urls:
                logger.warning(f"Check-in {checkin_id} ignorato: nessun file audio trovato.")
                continue

            audio_parts = []
            for url in audio_urls:
                file_name = url.split("/")[-1]
                try:
                    audio_bytes = supabase.storage.from_("audio_checkins").download(file_name)
                    audio_parts.append(types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"))
                except Exception as err:
                    logger.error(f"Errore nel download dell'audio {file_name}: {str(err)}")

            if client_ai and audio_parts:
                logger.info(f"Ri-analisi in corso per check-in {checkin_id} ({len(audio_parts)} audio)...")
                contents = [prompt] + audio_parts
                response = client_ai.models.generate_content(
                    model='gemini-3.8-flash',
                    contents=contents
                )
                
                full_text = response.text or ""

                # Separazione dell'analisi tecnica dalla bozza per il cliente
                if "---BOZZA_CLIENTE---" in full_text:
                    parts = full_text.split("---BOZZA_CLIENTE---")
                    new_analysis = parts[0].strip()
                    new_reply = parts[1].strip()
                else:
                    new_analysis = full_text.strip()
                    new_reply = "Ciao! Ho rianalizzato i tuoi aggiornamenti vocali. Proseguiamo così verso i nostri obiettivi!"

                # Aggiorna il DB salvando separatamente i due blocchi
                supabase.table("checkins").update({
                    "ai_analysis": new_analysis,
                    "reply_draft": new_reply
                }).eq("id", checkin_id).execute()

                processed_count += 1

        logger.info(f"==> [REANALYZE ALL] Completata la ri-analisi di {processed_count} check-in per il coach {coach_id}.")
        return jsonify({
            "success": True, 
            "message": f"Rigenerate con successo {processed_count} analisi!",
            "processed_count": processed_count
        }), 200

    except Exception as e:
        logger.error(f"==> [REANALYZE ALL] Errore critico durante la ri-analisi globale: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
