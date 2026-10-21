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
        "version": "2.2.1"
    })

# ------------------------ 1. OTTIENI TUTTI I DATI ADMIN (Cluster + Coach con relative associazioni) ------------------------
@app.route("/api/admin/data", methods=["GET"])
def admin_get_data():
    try:
        # Recupera tutti i cluster dalla tabella master 'clusters'
        clusters_res = supabase.table("clusters").select("*").order("id").execute()
        clusters = clusters_res.data or []

        logger.info(f"Elenco cluster recuperati dal database: {clusters}")
        
        # Recupera tutti i coach
        coaches_res = supabase.table("coaches").select("*").execute()
        coaches = coaches_res.data or []
        
        # Per ogni coach, recupera la lista di cluster_id attualmente associati
        for coach in coaches:
            assoc_res = supabase.table("coach_clusters").select("cluster_id").eq("coach_id", coach["id"]).execute()
            coach["cluster_ids"] = [item["cluster_id"] for item in assoc_res.data]

        return jsonify({
            "success": True,
            "clusters": clusters,
            "coaches": coaches
        })
    except Exception as e:
        logger.error(f"Errore recupero dati admin: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# ------------------------ 2. CREA UN NUOVO CLUSTER -----------------------------
@app.route("/api/admin/clusters", methods=["POST"])
def admin_create_cluster():
    try:
        data = request.json
        name = data.get("name", "").strip()
        description = data.get("description", "").strip()

        if not name:
            return jsonify({"error": "Il nome del cluster è obbligatorio."}), 400

        # Inserimento nel database
        res = supabase.table("clusters").insert({"name": name, "description": description}).execute()
        return jsonify({"success": True, "data": res.data})
    except Exception as e:
        logger.error(f"Errore creazione cluster: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# ------------------------ 3. ELIMINA UN CLUSTER --------------------------------------
@app.route("/api/admin/clusters/<int:cluster_id>", methods=["DELETE"])
def admin_delete_cluster(cluster_id):
    try:
        # La cancellazione in cascata su Postgres eliminerà in automatico le associazioni in coach_clusters
        supabase.table("clusters").delete().eq("id", cluster_id).execute()
        return jsonify({"success": True, "message": "Cluster eliminato con successo."})
    except Exception as e:
        logger.error(f"Errore eliminazione cluster {cluster_id}: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# ------------------------ 4. AGGIORNA ASSOCIAZIONI COACH <-> CLUSTER --------------------------
@app.route("/api/admin/coach-clusters", methods=["POST"])
def admin_update_coach_clusters():
    try:
        data = request.json
        coach_id = data.get("coach_id")
        selected_cluster_ids = data.get("cluster_ids", [])  # Lista degli ID selezionati

        if not coach_id:
            return jsonify({"error": "ID coach mancante."}), 400

        # 1. Rimuovi tutte le vecchie associazioni per questo coach
        supabase.table("coach_clusters").delete().eq("coach_id", coach_id).execute()

        # 2. Inserisci le nuove righe per i cluster selezionati (se ce ne sono)
        if selected_cluster_ids:
            new_rows = [{"coach_id": coach_id, "cluster_id": c_id} for c_id in selected_cluster_ids]
            supabase.table("coach_clusters").insert(new_rows).execute()

        return jsonify({"success": True, "message": "Associazioni aggiornate con successo."})
    except Exception as e:
        logger.error(f"Errore aggiornamento associazioni coach: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# ------------------------ FUNZIONE DI SUPPORTO CON LOGGING DEI CLUSTER ------------------------
def get_coach_prompt(coach_id):
    clusters = []
    logger.info(f"==> [AI PROMPT] Inizio  recupero cluster associati per coach_id: {coach_id}")
    
    try:
        # Interroga la tabella di join coach_clusters unendola alla tabella master clusters per estrarre i nomi
        c_res = supabase.table("coach_clusters") \
            .select("clusters(name)") \
            .eq("coach_id", coach_id) \
            .execute()
        
        if c_res.data:
            # Estrae i nomi dei cluster dalla relazione join
            clusters = [item["clusters"]["name"] for item in c_res.data if item.get("clusters")]
            logger.info(f"==> [AI PROMPT] Cluster trovati a DB per il coach: {clusters}")
        else:
            logger.warning(f"==> [AI PROMPT] Nessun cluster associato trovato per il coach {coach_id}.")
            
    except Exception as e:
        logger.error(f"==> [AI PROMPT] Errore recupero cluster: {str(e)}", exc_info=True)

    if not clusters:
        logger.info(f"==> [AI PROMPT] NESSUN CLUSTER ASSOCIATO, USO I DEFAULT")
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
            logger.info(f"==> [AI PROMPT] UPLOAD full_text: {full_text}")
            
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

        # 2. Recupera il prompt con le REGOLE TASSATIVE sui soli cluster associati
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

# --- ENDPOINT: Eliminazione di un singolo Check-in e relativi audio ---
@app.route("/api/coach/checkin/<checkin_id>", methods=["DELETE"])
def delete_checkin(checkin_id):
    logger.info(f"==> [DELETE CHECKIN] Richiesta eliminazione per check-in ID: {checkin_id}")
    try:
        # 1. Recupera il check-in per trovare i file audio associati
        res = supabase.table("checkins").select("*").eq("id", checkin_id).execute()
        if not res.data:
            return jsonify({"error": "Check-in non trovato"}), 404
        
        checkin_data = res.data[0]
        audio_urls = checkin_data.get("audio_urls") or ([checkin_data.get("audio_url")] if checkin_data.get("audio_url") else [])

        # 2. Elimina i file audio da Supabase Storage
        for url in audio_urls:
            if url:
                file_name = url.split("/")[-1]
                try:
                    supabase.storage.from_("audio_checkins").remove([file_name])
                    logger.info(f"File audio eliminato dallo storage: {file_name}")
                except Exception as storage_err:
                    logger.error(f"Errore eliminazione file storage {file_name}: {str(storage_err)}")

        # 3. Elimina il record dalla tabella checkins
        supabase.table("checkins").delete().eq("id", checkin_id).execute()
        logger.info(f"Check-in {checkin_id} eliminato con successo dal database.")

        return jsonify({"success": True, "message": "Check-in eliminato con successo!"}), 200

    except Exception as e:
        logger.error(f"==> [DELETE CHECKIN] Errore critico: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500
        
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
