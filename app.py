from flask import Flask, request, jsonify
import json
import asyncio
from datetime import datetime, timedelta
from like_pb2 import like
from uid_generator_pb2 import uid_generator
from like_count_pb2 import Info
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson
import aiohttp
import requests
import binascii
import os
from config import CONFIG
from cachetools import TTLCache
import threading
import time


app = Flask(__name__)

# Configuration

AUTH_URL ="https://api-jwt-token.vercel.app/token"
CACHE_DURATION = timedelta(hours=7).seconds  # Cache de 12 heures
TOKEN_REFRESH_THRESHOLD = timedelta(hours=6).seconds  # Rafraîchir avant expiration

SERVERS = {
    "ME": "https://clientbp.ggblueshark.com",
    "IND": "https://client.ind.freefiremobile.com",
    "BR": "https://client.us.freefiremobile.com",

}


class TokenCache:
    """Gestion améliorée du cache des tokens avec rafraîchissement automatique"""

    def __init__(self):
        self.cache = TTLCache(maxsize=100, ttl=CACHE_DURATION)
        self.last_refresh = {}
        self.lock = threading.Lock()
        self.session = requests.Session()

    def get_tokens(self, server_key):
        """Récupère les tokens du cache ou les regénère si nécessaire"""
        with self.lock:
            now = time.time()

            # Vérifier si on doit rafraîchir les tokens
            refresh_needed = (
                    server_key not in self.cache or
                    server_key not in self.last_refresh or
                    (now - self.last_refresh.get(server_key, 0)) > TOKEN_REFRESH_THRESHOLD)

            if refresh_needed:
                self._refresh_tokens(server_key)
                self.last_refresh[server_key] = now

            return self.cache.get(server_key, [])

    def _refresh_tokens(self, server_key):
        """Rafraîchit les tokens pour un serveur spécifique"""
        retry_delay= 1
        for i in range(2):
            try:
                creds = load_credentials(server_key)
                tokens = []

                # Utilisation d'une session partagée pour les requêtes
                for user in creds:
                    try:
                        params = {'uid': user['uid'], 'password': user['password']}
                        response = self.session.get(AUTH_URL, params=params, timeout=5)
                        if response.status_code == 200:
                            token = response.json().get("token")
                            if token:
                                tokens.append(token)
                    except Exception as e:
                        app.logger.error(f"Error fetching token for {user['uid']}: {str(e)}")
                        continue

                if tokens:
                    self.cache[server_key] = tokens
                    app.logger.info(f"Successfully refreshed tokens for {server_key}")
                else:
                    app.logger.warning(f"No valid tokens obtained for {server_key}")

            except Exception as e:
                app.logger.error(f"Error refreshing tokens for {server_key}: {str(e)}")
                time.sleep(retry_delay)
                retry_delay *= 2
                if server_key not in self.cache:
                    self.cache[server_key] = []


token_cache = TokenCache()


def load_credentials(server_key):
    """Charge les identifiants depuis le fichier de config"""
    with open(CONFIG[server_key], "r") as f:
        return json.load(f)


def encrypt_aes(data: bytes) -> str:
    """Chiffrement AES-CBC avec padding PKCS7"""
    key = b'Yg&tc%DEuh6%Zc^8'
    iv = b'6oyZDr22E3ychjM%'
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(data, AES.block_size)
    encrypted = cipher.encrypt(padded)
    return binascii.hexlify(encrypted).decode()


def create_uid_protobuf(uid: str):
    """Génère le protobuf pour l'UID"""
    msg = uid_generator()
    msg.saturn_ = int(uid)
    msg.garena = 1
    return msg.SerializeToString()


def create_like_protobuf(uid: str, region: str):
    """Génère le protobuf pour les likes"""
    msg = like()
    msg.uid = int(uid)
    msg.region = region
    return msg.SerializeToString()


def encode_uid(uid: str) -> str:
    """Encode l'UID pour les requêtes"""
    return encrypt_aes(create_uid_protobuf(uid))


def decode_info(data: bytes):
    """Décode la réponse protobuf du serveur"""
    try:
        info = Info()
        info.ParseFromString(data)
        return info
    except Exception:
        return None


def get_headers(token: str):
    """Retourne les headers HTTP nécessaires"""
    return {
        "User-Agent": "Dalvik/2.1.0",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Unity-Version": "2018.4.11f1",
        "X-GA": "v1 1",
        "ReleaseVersion": "OB48"
    }


def fetch_token(uid: str, password: str):
    """Récupère un token JWT depuis l'API d'authentification"""
    try:
        params = {'uid': uid, 'password': password}
        response = requests.get(AUTH_URL, params=params, timeout=5)
        if response.status_code == 200:
            return response.json().get("token")
        return None
    except Exception:
        return None


def make_request(uid_enc: str, url: str, token: str):
    """Effectue une requête POST au serveur Free Fire"""
    data = bytes.fromhex(uid_enc)
    headers = get_headers(token)
    try:
        response = requests.post(url, headers=headers, data=data, timeout=5, verify=False)
        if response.status_code == 200:
            return decode_info(response.content)
        return None
    except Exception:
        return None


async def send_like_requests(uid: str, region: str, tokens: list, url: str):
    """Envoie les requêtes de like asynchrones"""
    encrypted = encrypt_aes(create_like_protobuf(uid, region))
    data = bytes.fromhex(encrypted)

    async def send(token):
        try:
            headers = get_headers(token)
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=data, headers=headers) as resp:
                    return await resp.text()
        except Exception:
            return None

    tasks = [send(token) for token in tokens]
    return await asyncio.gather(*tasks)


@app.route("/like", methods=["GET"])
def like_player():
    """Endpoint principal pour envoyer des likes"""
    uid = request.args.get("uid")
    # Input validation
    if not uid:
        return jsonify({
            "error": "Missing UID",
            "message": "UID parameter is required",
            "timestamp": datetime.utcnow().isoformat(),
            "status": 400
        }), 400
    
    if not uid.isdigit():
        return jsonify({
            "error": "Invalid UID",
            "message": "UID must contain only digits ",
            "received_uid": uid,
            "timestamp": datetime.utcnow().isoformat(),
            "status": 400
        }), 400

    # Détection du serveur du joueur
    region = None
    player_info = None
    for current_region in SERVERS:
        tokens = token_cache.get_tokens(current_region)
        if not tokens:
            continue

        server_url = SERVERS[current_region]
        info_url = f"{server_url}/GetPlayerPersonalShow"
        player_info = make_request(encode_uid(uid), info_url, tokens[0])

        if player_info:
            region = current_region
            break

    if not player_info:
        return jsonify({
            "error": "Player not found",
            "message": "Player not found on any server",
            "status": 404
        }), 404

    # Envoi des likes
    before_likes = player_info.AccountInfo.Likes
    player_name = player_info.AccountInfo.PlayerNickname
    like_url = f"{SERVERS[region]}/LikeProfile"

    # Envoi asynchrone des likes
    results = asyncio.run(send_like_requests(uid, region, token_cache.get_tokens(region), like_url))

    # Vérification du résultat
    new_info = make_request(encode_uid(uid), info_url, token_cache.get_tokens(region)[0])
    after_likes = new_info.AccountInfo.Likes if new_info else before_likes



    return jsonify({
        "Credits": "https://t.me/nopethug",
        "player": player_name,
        "uid": uid,
        "likes_before": before_likes,
        "likes_after": after_likes,
        "likes_added": after_likes - before_likes,
        "server_region": region,
        "status": 1 if after_likes > before_likes else 2,
        "timestamp": datetime.utcnow().isoformat(),

    })


@app.route("/health-check", methods=["GET"])
def health_check():
    """Service health check endpoint"""
    try:
        # Verify tokens are available for each server
        for server in SERVERS:
            tokens = token_cache.get_tokens(server)
            if not tokens:
                return jsonify({
                    "status": "unhealthy",
                    "message": f"No tokens available for {server}",
                    "timestamp": datetime.utcnow().isoformat()
                }), 500

        return jsonify({
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }), 500

if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("PORT", 5000))
    serve(app, host="0.0.0.0", port=port)
