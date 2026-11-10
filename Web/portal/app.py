import os
import re
import socket
import unicodedata
import docker
from docker.errors import DockerException, NotFound, APIError
from flask import Flask, jsonify, request, render_template

import db

app = Flask(__name__)

# Initialisation des tables MariaDB au démarrage
db.init_database()


def get_docker_client():
    """Initialise et retourne le client Docker SDK."""
    try:
        return docker.from_env()
    except DockerException as e:
        app.logger.error(f"Impossible de se connecter au démon Docker: {e}")
        return None


def slugify(text: str) -> str:
    """Nettoie un texte pour en faire un identifiant URL/Docker valide."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("utf-8")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "app"


def is_port_in_use(port: int) -> bool:
    """Vérifie si un port réseau est déjà utilisé sur la machine."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def find_available_port(start_port=8081) -> int:
    """Trouve le premier port libre pour exposer directement le conteneur."""
    deployments = db.get_all_deployments()
    used_ports = {int(d["host_port"]) for d in deployments if d.get("host_port")}
    port = start_port
    while (port in used_ports or is_port_in_use(port)) and port < 65535:
        port += 1
    return port


@app.route("/")
def index():
    """Page d'accueil du portail."""
    return render_template("index.html")


@app.route("/api/catalog", methods=["GET"])
def get_catalog():
    """Récupère les images disponibles directement depuis la base MariaDB."""
    apps = db.get_catalog()
    return jsonify({"catalog": apps})


@app.route("/api/deployments", methods=["GET"])
def list_deployments():
    """Liste tous les conteneurs créés et leur état en temps réel depuis Docker."""
    client = get_docker_client()
    deployments = []
    seen_slugs = set()

    # 1. Lecture directe depuis Docker via les labels
    if client:
        try:
            managed_containers = client.containers.list(all=True, filters={"label": "appfactory.managed=true"})
            for c in managed_containers:
                slug = c.labels.get("appfactory.slug") or c.name.replace("af-", "")
                app_name = c.labels.get("appfactory.app_name") or slug
                docker_image = c.labels.get("appfactory.image") or (c.image.tags[0] if c.image.tags else "")
                host_port = c.labels.get("appfactory.host_port") or ""
                url = c.labels.get("appfactory.url") or (f"http://localhost:{host_port}" if host_port else "")
                st = "ok" if c.status == "running" else ("wait" if c.status in ("restarting", "created") else "off")

                deployments.append({
                    "slug": slug,
                    "app": app_name,
                    "docker_image": docker_image,
                    "container_name": c.name,
                    "container_id": c.short_id,
                    "host_port": host_port,
                    "url": url,
                    "st": st
                })
                seen_slugs.add(slug)
        except Exception as e:
            app.logger.warning(f"Erreur lecture conteneurs Docker: {e}")

    # 2. Complément éventuel depuis la BDD si la table deployments existe
    try:
        db_records = db.get_all_deployments()
        for d in db_records:
            if d.get("slug") not in seen_slugs:
                deployments.append(d)
                seen_slugs.add(d.get("slug"))
    except Exception:
        pass

    return jsonify({"deployments": deployments})


@app.route("/api/deployments", methods=["POST"])
def create_deployment():
    """
    1. Récupère l'image et sa config lues depuis MariaDB.
    2. Alloue un port hôte libre (8081, 8082, etc.).
    3. Lance le conteneur Docker sans aucun reverse proxy.
    """
    client = get_docker_client()
    if not client:
        return jsonify({"error": "Démon Docker inaccessible sur le système"}), 503

    data = request.get_json(silent=True) or {}
    app_id = data.get("app_id")

    # 1. Lecture des informations de l'application depuis MariaDB
    app_config = db.get_application(app_id)
    if not app_config:
        return jsonify({"error": f"L'application '{app_id}' n'existe pas dans la base MariaDB"}), 404

    docker_image = app_config.get("docker_image") or app_config.get("image")
    if not docker_image:
        return jsonify({"error": f"Image Docker non renseignée pour l'application '{app_id}'"}), 400

    app_name = app_config.get("name") or app_id
    base_slug = slugify(app_name)
    existing = [c.name for c in client.containers.list(all=True)]
    slug = base_slug
    idx = 2
    while f"af-{slug}" in existing:
        slug = f"{base_slug}-{idx}"
        idx += 1

    container_name = f"af-{slug}"
    volume_name = f"af-{slug}-data"
    internal_port = int(app_config.get("internal_port") or 80)
    volume_path = app_config.get("volume_path")

    # 2. Allocation d'un port local dédié sur le Mac
    host_port = find_available_port(start_port=8081)
    direct_url = f"http://localhost:{host_port}"

    try:
        # 3. Création et démarrage du conteneur avec redirection de port directe
        container_kwargs = {
            "image": docker_image,
            "name": container_name,
            "detach": True,
            "ports": {f"{internal_port}/tcp": host_port},
            "nano_cpus": 1_000_000_000,   # 1 vCPU
            "mem_limit": "512m",          # 512 Mo RAM
            "labels": {
                "appfactory.managed": "true",
                "appfactory.slug": slug,
                "appfactory.app_id": str(app_id),
                "appfactory.app_name": str(app_name),
                "appfactory.image": str(docker_image),
                "appfactory.host_port": str(host_port),
                "appfactory.url": direct_url
            },
            "restart_policy": {"Name": "unless-stopped"}
        }

        if volume_path:
            container_kwargs["volumes"] = {volume_name: {"bind": volume_path, "mode": "rw"}}

        container = client.containers.run(**container_kwargs)

        # 4. Enregistrement facultatif si la table BDD existe
        try:
            db.record_deployment(
                slug=slug,
                app_id=str(app_id),
                docker_image=docker_image,
                container_name=container_name,
                container_id=container.short_id,
                host_port=host_port,
                url=direct_url,
                status="ok"
            )
        except Exception:
            pass

        return jsonify({
            "message": f"Conteneur {app_name} lancé avec succès sur le port {host_port}",
            "slug": slug,
            "container_id": container.short_id,
            "docker_image": docker_image,
            "host_port": host_port,
            "url": direct_url,
            "st": "ok"
        }), 201

    except APIError as e:
        app.logger.error(f"Erreur Docker run: {e}")
        return jsonify({"error": f"Erreur lors du lancement Docker : {str(e)}"}), 500
        return jsonify({"error": f"Erreur lors du lancement Docker : {str(e)}"}), 500


@app.route("/api/deployments/<slug>/toggle", methods=["POST"])
def toggle_deployment(slug):
    """Démarre ou arrête le conteneur directement."""
    client = get_docker_client()
    if not client:
        return jsonify({"error": "Démon Docker inaccessible"}), 503

    container_name = f"af-{slug}"
    try:
        container = client.containers.get(container_name)
    except NotFound:
        return jsonify({"error": f"Conteneur '{slug}' introuvable"}), 404

    try:
        if container.status == "running":
            container.stop(timeout=5)
            new_st = "off"
        else:
            container.start()
            new_st = "ok"

        db.update_deployment_status(slug, new_st)
        return jsonify({"slug": slug, "status": new_st})
    except APIError as e:
        return jsonify({"error": f"Erreur modification état: {str(e)}"}), 500


@app.route("/api/deployments/<slug>", methods=["DELETE"])
def delete_deployment(slug):
    """Supprime le conteneur, son volume de données, et l'entrée dans MariaDB."""
    client = get_docker_client()
    if not client:
        return jsonify({"error": "Démon Docker inaccessible"}), 503

    container_name = f"af-{slug}"
    volume_name = f"af-{slug}-data"

    try:
        container = client.containers.get(container_name)
        container.remove(force=True)
    except NotFound:
        pass
    except APIError as e:
        return jsonify({"error": f"Erreur suppression conteneur: {str(e)}"}), 500

    # Nettoyage du volume persistant
    try:
        vol = client.volumes.get(volume_name)
        vol.remove(force=True)
    except Exception:
        pass

    # Suppression de la BDD
    db.delete_deployment_record(slug)

    return jsonify({"message": f"Conteneur '{slug}' et son volume supprimés avec succès"})


@app.route("/api/deployments/<slug>/logs", methods=["GET"])
def get_deployment_logs(slug):
    """Récupère les logs Docker en direct du conteneur."""
    client = get_docker_client()
    if not client:
        return jsonify({"error": "Démon Docker inaccessible"}), 503

    container_name = f"af-{slug}"
    try:
        container = client.containers.get(container_name)
        logs = container.logs(tail=100, timestamps=True).decode("utf-8", errors="replace")
        return jsonify({"slug": slug, "logs": logs})
    except NotFound:
        return jsonify({"error": f"Conteneur '{slug}' introuvable"}), 404
    except APIError as e:
        return jsonify({"error": f"Erreur lecture logs: {str(e)}"}), 500


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("🚀 Port App Factory démarré sur http://localhost:5000")
    print("   Connexion MariaDB : factory_user@127.0.0.1:3306/app_factory_db")
    print("   (Sans aucun reverse proxy - ports directs http://localhost:8081...)")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=True)
