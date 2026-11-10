import os
import time
import logging
import pymysql
from pymysql.cursors import DictCursor

logger = logging.getLogger(__name__)

# Configuration de connexion à la base de données
DB_HOST = os.getenv("DB_HOST", "app_factory_db")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "factory_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "user_password_factory")
DB_NAME = os.getenv("DB_NAME", "app_factory_db")


def get_db_connection(retries=3, delay=2):
    """Établit la connexion à MariaDB avec repli automatique (app_factory_db, db, 127.0.0.1)."""
    hosts_to_try = [DB_HOST]
    for candidate in ["app_factory_db", "db", "127.0.0.1", "host.docker.internal"]:
        if candidate not in hosts_to_try:
            hosts_to_try.append(candidate)

    last_err = None
    for attempt in range(retries):
        for host in hosts_to_try:
            try:
                return pymysql.connect(
                    host=host,
                    port=DB_PORT,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    database=DB_NAME,
                    cursorclass=DictCursor,
                    autocommit=True,
                    connect_timeout=3
                )
            except pymysql.MySQLError as e:
                last_err = e
        if attempt < retries - 1:
            time.sleep(delay)
    raise last_err


def init_database():
    """Vérifie la connexion en lecture à MariaDB."""
    try:
        conn = get_db_connection(retries=5, delay=2)
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
        conn.close()
        logger.info("Connexion MariaDB vérifiée en lecture.")
        return True
    except Exception as e:
        logger.warning(f"MariaDB non joignable pour le moment : {e}")
        return False


def get_catalog():
    """Lit les applications disponibles dans la table 'applications' de MariaDB."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, description, docker_image, 
                       COALESCE(internal_port, 80) AS internal_port, 
                       COALESCE(volume_path, '') AS volume_path, 
                       COALESCE(default_cpu, '1 vCPU') AS default_cpu, 
                       COALESCE(default_ram, '512 Mo') AS default_ram
                FROM applications
                ORDER BY name ASC
            """)
            apps = cur.fetchall()
        conn.close()
        return apps
    except Exception as e:
        # Repli si colonnes plus simples
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM applications")
                apps = cur.fetchall()
            conn.close()
            return apps
        except Exception:
            logger.error(f"Erreur lecture get_catalog: {e}")
            return []


def get_application(app_id: str):
    """Lit les informations d'une image applicative depuis MariaDB."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM applications WHERE id = %s", (app_id,))
            app = cur.fetchone()
        conn.close()
        return app
    except Exception as e:
        logger.error(f"Erreur lecture get_application '{app_id}': {e}")
        return None


def get_all_deployments():
    """Retourne tous les déploiements enregistrés dans MariaDB."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT d.slug, d.app_id, a.name AS app, d.docker_image, d.container_name, 
                       d.container_id, d.host_port, d.url, d.status AS st, d.created_at
                FROM deployments d
                LEFT JOIN applications a ON d.app_id = a.id
                ORDER BY d.created_at DESC
            """)
            rows = cur.fetchall()
        conn.close()
        for r in rows:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
        return rows
    except Exception as e:
        logger.error(f"Erreur get_all_deployments: {e}")
        return []


def record_deployment(slug, app_id, docker_image, container_name, container_id, host_port, url, status="wait"):
    """Enregistre un nouveau conteneur créé avec son port d'écoute."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO deployments (slug, app_id, docker_image, container_name, container_id, host_port, url, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    container_id = VALUES(container_id),
                    host_port = VALUES(host_port),
                    url = VALUES(url),
                    status = VALUES(status),
                    updated_at = CURRENT_TIMESTAMP
            """, (slug, app_id, docker_image, container_name, container_id, host_port, url, status))
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Erreur record_deployment '{slug}': {e}")
        return False


def update_deployment_status(slug, status):
    """Met à jour le statut du conteneur dans MariaDB."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE deployments SET status = %s WHERE slug = %s", (status, slug))
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Erreur update_deployment_status '{slug}': {e}")
        return False


def delete_deployment_record(slug):
    """Supprime un déploiement de MariaDB."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM deployments WHERE slug = %s", (slug,))
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Erreur delete_deployment_record '{slug}': {e}")
        return False
