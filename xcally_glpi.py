#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xCally -> GLPI : ventana emergente para registrar la llamada y abrir el ticket.

Funcionamiento
--------------
El programa queda residente escuchando en 127.0.0.1:<puerto>. Cuando xCally
avisa de una llamada entrante (Trigger -> URL, MotionBar, script AMI, etc.)
se abre UNA sola ventana con el telefono ya rellenado. Mientras la llamada
sigue viva los avisos repetidos se ignoran (deduplicacion por uniqueid).

Uso:
    python xcally_glpi.py                    # modo residente
    pythonw xcally_glpi.py                   # residente sin consola (Windows)
    python xcally_glpi.py --test 912345678   # abre la ventana de prueba
    python xcally_glpi.py --ping 912345678   # simula una llamada contra el listener
    python xcally_glpi.py --check-glpi       # valida credenciales GLPI
    python xcally_glpi.py --retry-pending    # reintenta tickets que fallaron
"""

import argparse
import json
import mimetypes
import os
import queue
import re
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    from PIL import ImageGrab
    PIL_OK = True
except Exception:
    PIL_OK = False


for _stream in (sys.stdout, sys.stderr):     # acentos legibles en la consola de Windows
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("XCALLY_GLPI_CONFIG", os.path.join(APP_DIR, "config.json"))
APP_NAME = "Registro de llamada - xCally / GLPI"
CONFIG = {}

DEFAULT_CONFIG = {
    "listener": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8765,
        "token": ""
    },
    "phonebar": {
        "enabled": True,
        "log_file": "",
        "evento_apertura": "connect",
        "solo_entrantes": True,
        "agente": "",
        "colas": [],
        "poll_seconds": 1.0
    },
    "glpi": {
        "url": "https://glpi.midominio.cl/apirest.php",
        "app_token": "",
        "user_token": "",
        "user": "",
        "password": "",
        "verify_ssl": True,
        "timeout": 30,
        "entities_id": 0,
        "type": 1,
        "urgency": 3,
        "itilcategories_id": 0,
        "requesttypes_id": 3,
        "users_id_requester": 0,
        "users_id_assign": 0,
        "groups_id_assign": 0
    },
    "call": {
        "dedup_seconds": 300,
        "single_window": True,
        "strip_prefixes": ["+56", "56", "0"],
        "min_digits": 6
    },
    "ui": {
        "always_on_top": True,
        "title_template": "{sucursal} - {resumen}",
        "resumen_max": 70,
        "sucursales": [],
        "sucursales_locations": {},
        "entidades": {},
        "por_entidad": {},
        "correos_usuarios": {},
        "dominios_correo": [],
        "dominio_por_entidad": {},
        "entidad_por_dominio": {},
        "telefono_sucursal": {},
        "categorias": {},
        "categoria_preferida": "",
        "titulo_etiquetas_ignorar": [
            "motivo", "asunto", "detalle", "descripcion", "problema",
            "consulta", "resumen", "observacion", "novedad"
        ],
        "titulo_frases_ignorar": [
            "buenos dias", "buenas tardes", "buenas noches", "hola",
            "estimados", "estimado", "estimada",
            "el cliente indica que", "cliente indica que",
            "el cliente informa que", "cliente informa que",
            "el cliente reporta que", "cliente reporta que",
            "el cliente comenta que", "cliente comenta que",
            "el cliente menciona que", "cliente menciona que",
            "el cliente manifiesta que", "cliente manifiesta que",
            "el cliente solicita que", "el cliente solicita", "cliente solicita",
            "el cliente requiere que", "el cliente requiere", "cliente requiere",
            "el cliente necesita que", "el cliente necesita", "cliente necesita",
            "el usuario indica que", "usuario indica que",
            "el usuario informa que", "usuario informa que",
            "el usuario reporta que", "usuario reporta que",
            "el usuario solicita que", "usuario solicita",
            "el usuario requiere que", "usuario requiere",
            "el usuario necesita que", "usuario necesita",
            "llama para informar que", "llama para indicar que",
            "llama para solicitar", "llama para consultar", "llama para reportar",
            "llama porque", "llama por",
            "se comunica para", "se contacta para",
            "contacta para informar que", "contacta para solicitar",
            "solicita que", "requiere que", "necesita que",
            "informa que", "indica que", "menciona que", "comenta que",
            "reporta que", "senala que", "manifiesta que",
            "consulta si", "consulta por", "pregunta si", "pregunta por"
        ],
        "urgencias": {
            "Muy baja": 1,
            "Baja": 2,
            "Media": 3,
            "Alta": 4,
            "Muy alta": 5
        }
    },
    "xcally": {
        "enabled": False,
        "base_url": "",
        "token": "",
        "poll_endpoint": "/api/voice/channels",
        "poll_seconds": 3,
        "agent_filter": "",
        "number_fields": ["calleridnum", "callerid", "source", "from", "cid", "caller"],
        "id_fields": ["uniqueid", "linkedid", "callid", "id"],
        "verify_ssl": True
    },
    "log": {
        "file": "llamadas.log",
        "pending_dir": "pendientes"
    }
}


# --------------------------------------------------------------------------- #
# Configuracion / utilidades
# --------------------------------------------------------------------------- #
def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_log_lock = threading.Lock()


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _log_lock:
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            name = (CONFIG.get("log", {}) or {}).get("file") or "llamadas.log"
            with open(os.path.join(APP_DIR, name), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_CONFIG, fh, indent=2, ensure_ascii=False)
        log("Se creo el archivo de configuracion: %s" % CONFIG_PATH)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
    except Exception as exc:
        log("ERROR leyendo %s: %s. Se usan valores por defecto." % (CONFIG_PATH, exc))
        user_cfg = {}
    return deep_merge(DEFAULT_CONFIG, user_cfg)


def normalize_number(raw, cfg):
    """Deja solo digitos y quita prefijos de pais / salida para poder comparar."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", str(raw))
    for pref in cfg.get("call", {}).get("strip_prefixes", []):
        p = re.sub(r"\D", "", str(pref))
        if p and digits.startswith(p) and len(digits) - len(p) >= 8:
            digits = digits[len(p):]
            break
    return digits


def pretty_number(raw):
    return "" if raw is None else str(raw).strip()


def esc(s):
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


# --------------------------------------------------------------------------- #
# Resumen automatico del titulo (boton "Auto")
# --------------------------------------------------------------------------- #
_TITULO_SENT_RE = re.compile(r"(?<=[.!?;])\s+")


def _normalizar(s):
    """minusculas y sin tildes, solo para comparar (no altera el texto real)."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


def fragmentos_descripcion(desc):
    """Parte la descripcion en oraciones/lineas candidatas, en orden."""
    for linea in (desc or "").splitlines():
        linea = linea.strip()
        if not linea:
            continue
        for frag in _TITULO_SENT_RE.split(linea):
            frag = frag.strip(" .!?;,")
            if frag:
                yield frag


def limpiar_resumen(cfg, texto):
    """Quita saludos, etiquetas ('Motivo:') y frases de relleno del inicio
    ('el cliente indica que...') para quedarse con el motivo real de la llamada."""
    etiquetas = [_normalizar(e) for e in (cfg.get("ui", {}).get("titulo_etiquetas_ignorar") or [])]
    frases = [_normalizar(f) for f in (cfg.get("ui", {}).get("titulo_frases_ignorar") or [])]
    out = (texto or "").strip()
    cambiado = True
    while out and cambiado:
        cambiado = False
        plano = _normalizar(out)
        for etq in etiquetas:
            if etq and plano.startswith(etq + ":"):
                out = out[len(etq) + 1:].lstrip(" ,:;-").strip()
                cambiado = True
                break
        if cambiado:
            continue
        for f in frases:
            if f and plano.startswith(f):
                out = out[len(f):].lstrip(" ,:;-").strip()
                cambiado = True
                break
    return re.sub(r"\s+", " ", out).strip()


# --------------------------------------------------------------------------- #
# Cliente GLPI (API REST /apirest.php)
# --------------------------------------------------------------------------- #
class GlpiError(Exception):
    pass


class GlpiClient:
    def __init__(self, cfg):
        if requests is None:
            raise GlpiError("Falta la libreria 'requests'. Instalar con: pip install requests")
        g = cfg["glpi"]
        self.base = (g.get("url") or "").rstrip("/")
        if not self.base:
            raise GlpiError("Configura glpi.url (ej: https://glpi.tuempresa.cl/apirest.php)")
        self.app_token = (g.get("app_token") or "").strip()
        self.user_token = (g.get("user_token") or "").strip()
        self.user = g.get("user") or ""
        self.password = g.get("password") or ""
        self.verify = bool(g.get("verify_ssl", True))
        self.timeout = int(g.get("timeout", 30))
        self.session_token = None
        self.sess = requests.Session()

    def _headers(self, json_ct=True):
        h = {}
        if json_ct:
            h["Content-Type"] = "application/json"
        if self.app_token:
            h["App-Token"] = self.app_token
        if self.session_token:
            h["Session-Token"] = self.session_token
        return h

    def init_session(self):
        headers = {"Content-Type": "application/json"}
        if self.app_token:
            headers["App-Token"] = self.app_token
        auth = None
        if self.user_token:
            headers["Authorization"] = "user_token %s" % self.user_token
        elif self.user:
            auth = (self.user, self.password)
        else:
            raise GlpiError("Configura glpi.user_token (recomendado) o glpi.user/password.")
        r = self.sess.get("%s/initSession" % self.base, headers=headers, auth=auth,
                          verify=self.verify, timeout=self.timeout)
        if r.status_code != 200:
            raise GlpiError("initSession %s: %s" % (r.status_code, r.text[:400]))
        self.session_token = (r.json() or {}).get("session_token")
        if not self.session_token:
            raise GlpiError("initSession sin session_token: %s" % r.text[:400])
        return self.session_token

    def kill_session(self):
        if not self.session_token:
            return
        try:
            self.sess.get("%s/killSession" % self.base, headers=self._headers(),
                          verify=self.verify, timeout=self.timeout)
        except Exception:
            pass
        self.session_token = None

    def get(self, path, **params):
        url = "%s/%s" % (self.base, path.lstrip("/"))
        r = self.sess.get(url, headers=self._headers(), params=params,
                          verify=self.verify, timeout=self.timeout)
        if r.status_code not in (200, 206):
            raise GlpiError("GET %s -> %s: %s" % (path, r.status_code, r.text[:300]))
        return r.json()

    def create_ticket(self, fields):
        r = self.sess.post("%s/Ticket/" % self.base, headers=self._headers(),
                           data=json.dumps({"input": fields}).encode("utf-8"),
                           verify=self.verify, timeout=self.timeout)
        if r.status_code not in (200, 201):
            raise GlpiError("Crear ticket %s: %s" % (r.status_code, r.text[:600]))
        data = r.json()
        if isinstance(data, list):
            data = data[0] if data else {}
        tid = (data or {}).get("id")
        if not tid:
            raise GlpiError("Respuesta inesperada al crear ticket: %s" % r.text[:400])
        return int(tid)

    def upload_document(self, path, ticket_id):
        """Sube un archivo y lo asocia al ticket."""
        name = os.path.basename(path)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        manifest = {"input": {"name": name,
                              "_filename": [name],
                              "itemtype": "Ticket",
                              "items_id": ticket_id}}
        headers = self._headers(json_ct=False)  # requests define el multipart
        with open(path, "rb") as fh:
            files = {
                "uploadManifest": (None, json.dumps(manifest), "application/json"),
                "filename[0]": (name, fh, ctype),
            }
            r = self.sess.post("%s/Document/" % self.base, headers=headers, files=files,
                               verify=self.verify, timeout=max(self.timeout, 120))
        if r.status_code not in (200, 201):
            raise GlpiError("Subir '%s' %s: %s" % (name, r.status_code, r.text[:400]))
        data = r.json()
        if isinstance(data, list):
            data = data[0] if data else {}
        doc_id = (data or {}).get("id")
        if doc_id:
            self._ensure_link(doc_id, ticket_id)
        return doc_id

    def _ensure_link(self, doc_id, ticket_id):
        """Algunas versiones de GLPI ignoran items_id del manifest."""
        try:
            r = self.sess.get("%s/Document/%s/Document_Item/" % (self.base, doc_id),
                              headers=self._headers(), verify=self.verify,
                              timeout=self.timeout)
            if r.status_code == 200:
                for row in (r.json() or []):
                    if (str(row.get("itemtype")) == "Ticket"
                            and int(row.get("items_id") or 0) == int(ticket_id)):
                        return
        except Exception:
            pass
        try:
            self.sess.post("%s/Document_Item/" % self.base, headers=self._headers(),
                           data=json.dumps({"input": {"documents_id": doc_id,
                                                      "itemtype": "Ticket",
                                                      "items_id": ticket_id}}).encode("utf-8"),
                           verify=self.verify, timeout=self.timeout)
        except Exception as exc:
            log("Aviso: no se pudo vincular documento %s al ticket %s: %s"
                % (doc_id, ticket_id, exc))


def buscar_usuario_por_correo(cfg, correo):
    """Consulta la API de GLPI: correo -> (users_id, nombre). (0, '') si no existe."""
    c = GlpiClient(cfg)
    c.init_session()
    try:
        r = c.sess.get("%s/UserEmail" % c.base, headers=c._headers(),
                       params={"searchText[email]": correo},
                       verify=c.verify, timeout=c.timeout)
        hits = r.json() if r.status_code in (200, 206) else []
        if not isinstance(hits, list) or not hits:
            return 0, ""
        uid = int(hits[0].get("users_id") or 0)
        if not uid:
            return 0, ""
        u = c.get("User/%s" % uid) or {}
        nombre = " ".join(x for x in (u.get("firstname"), u.get("realname")) if x)
        return uid, nombre or u.get("name") or ""
    finally:
        c.kill_session()


def submit_to_glpi(cfg, ticket_fields, attachments, progress=None):
    """Crea el ticket y sube los adjuntos. Devuelve (ticket_id, errores_adjuntos)."""
    def say(m):
        if progress:
            progress(m)
        log(m)

    client = GlpiClient(cfg)
    say("Conectando con GLPI...")
    client.init_session()
    try:
        say("Creando ticket...")
        tid = client.create_ticket(ticket_fields)
        say("Ticket #%s creado." % tid)
        errors = []
        for i, path in enumerate(attachments, 1):
            try:
                say("Subiendo adjunto %d/%d: %s" % (i, len(attachments), os.path.basename(path)))
                client.upload_document(path, tid)
            except Exception as exc:
                errors.append("%s: %s" % (os.path.basename(path), exc))
                log("ERROR adjunto: %s" % exc)
        return tid, errors
    finally:
        client.kill_session()


def save_pending(cfg, fields, attachments):
    try:
        d = os.path.join(APP_DIR, cfg["log"].get("pending_dir", "pendientes"))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "ticket_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"fields": fields, "attachments": attachments}, fh,
                      indent=2, ensure_ascii=False)
        return path
    except Exception as exc:
        log("No se pudo guardar el borrador: %s" % exc)
        return None


# --------------------------------------------------------------------------- #
# Ventana del formulario
# --------------------------------------------------------------------------- #
class TicketWindow(tk.Toplevel):
    def __init__(self, master, cfg, call, on_close):
        super().__init__(master)
        self.cfg = cfg
        self.call = call
        self.on_close_cb = on_close
        self.attachments = []
        self.title_manual = False
        self.dominio_manual = False
        self.categoria_manual = False
        self._ajustando = False
        self.sending = False
        self.closed = False
        self._tmpdir = None

        self.title(APP_NAME)
        self.geometry("700x545")
        self.minsize(640, 470)
        if cfg["ui"].get("always_on_top", True):
            self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.cancel)

        self._build()
        self._prefill()
        self.after(200, self._focus_me)

    # -- construccion ------------------------------------------------------ #
    def _build(self):
        """Diseno compacto: dos columnas de campos, una sola fila de listas."""
        pad = {"padx": 8, "pady": 3}
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.columnconfigure(3, weight=1)

        num = pretty_number(self.call.get("number")) or "numero desconocido"
        cab = ttk.Frame(root)
        cab.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 6))
        ttk.Label(cab, text="Llamada entrante", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Label(cab, text="  %s" % num, font=("Segoe UI", 11, "bold"),
                  foreground="#1d4ed8").pack(side="left")
        ttk.Label(cab, text="   %s" % self.call.get("ts_str", ""),
                  foreground="#888").pack(side="left")

        r = 1
        ttk.Label(root, text="Nombre *").grid(row=r, column=0, sticky="w", **pad)
        self.e_nombre = ttk.Entry(root)
        self.e_nombre.grid(row=r, column=1, sticky="ew", **pad)
        self.e_nombre.bind("<KeyRelease>", self._refresh_title)
        ttk.Label(root, text="Telefono *").grid(row=r, column=2, sticky="w", **pad)
        self.e_telefono = ttk.Entry(root, width=18)
        self.e_telefono.grid(row=r, column=3, sticky="ew", **pad)
        r += 1

        # Correo del solicitante: se escribe solo el usuario y se elige el dominio.
        dominios = self.cfg["ui"].get("dominios_correo") or ["@"]
        ttk.Label(root, text="Correo").grid(row=r, column=0, sticky="w", **pad)
        marco = ttk.Frame(root)
        marco.grid(row=r, column=1, sticky="ew", **pad)
        marco.columnconfigure(0, weight=1)
        self.e_usuario = ttk.Entry(marco)
        self.e_usuario.grid(row=0, column=0, sticky="ew")
        self.e_usuario.bind("<KeyRelease>", self._on_correo)
        self.cb_dominio = ttk.Combobox(marco, values=dominios, width=17, state="readonly")
        self.cb_dominio.grid(row=0, column=1, padx=(3, 0))
        self.cb_dominio.set(dominios[0])
        self.cb_dominio.bind("<<ComboboxSelected>>", self._on_dominio)
        self.lbl_correo = ttk.Label(root, text="solicitante por defecto", foreground="#888")
        self.lbl_correo.grid(row=r, column=2, columnspan=2, sticky="w", padx=8)
        r += 1

        ttk.Label(root, text="Sucursal *").grid(row=r, column=0, sticky="w", **pad)
        self.cb_sucursal = ttk.Combobox(root, values=self.cfg["ui"].get("sucursales", []))
        self.cb_sucursal.grid(row=r, column=1, columnspan=3, sticky="ew", **pad)
        self.cb_sucursal.bind("<KeyRelease>", self._refresh_title)
        self.cb_sucursal.bind("<<ComboboxSelected>>", self._refresh_title)
        r += 1

        # Entidad + urgencia + categoria, todo en una sola fila.
        urg = self.cfg["ui"].get("urgencias", {}) or {}
        cats = self.cfg["ui"].get("categorias", {}) or {}
        ents = self.cfg["ui"].get("entidades", {}) or {}
        fila = ttk.Frame(root)
        fila.grid(row=r, column=0, columnspan=4, sticky="ew", padx=8, pady=3)

        if ents:
            ttk.Label(fila, text="Entidad").pack(side="left")
            self.cb_entidad = ttk.Combobox(fila, values=list(ents.keys()), width=14,
                                           state="readonly")
            self.cb_entidad.pack(side="left", padx=(4, 12))
            self.cb_entidad.bind("<<ComboboxSelected>>", self._on_entidad)
        else:
            self.cb_entidad = None

        ttk.Label(fila, text="Urgencia").pack(side="left")
        self.cb_urgencia = ttk.Combobox(fila, values=list(urg.keys()), width=10,
                                        state="readonly")
        self.cb_urgencia.pack(side="left", padx=(4, 12))

        if cats or ents:
            ttk.Label(fila, text="Categoria").pack(side="left")
            self.cb_categoria = ttk.Combobox(fila, values=[""] + list(cats.keys()),
                                             width=20, state="readonly")
            self.cb_categoria.pack(side="left", padx=4)
            self.cb_categoria.bind("<<ComboboxSelected>>", self._on_categoria_manual)
        else:
            self.cb_categoria = None

        self.lbl_entidad = ttk.Label(root, text="", foreground="#888")
        self.lbl_entidad.grid(row=r + 1, column=0, columnspan=4, sticky="w", padx=10)
        r += 2

        ttk.Separator(root).grid(row=r, column=0, columnspan=4, sticky="ew", pady=6)
        r += 1

        ttk.Label(root, text="Titulo *").grid(row=r, column=0, sticky="w", **pad)
        self.e_titulo = ttk.Entry(root)
        self.e_titulo.grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        self.e_titulo.bind("<Key>", self._title_touched)
        ttk.Button(root, text="Auto", width=6, command=self._force_title).grid(
            row=r, column=3, sticky="w", padx=8)
        r += 1

        ttk.Label(root, text="Descripcion *").grid(row=r, column=0, sticky="nw", **pad)
        caja = ttk.Frame(root)
        caja.grid(row=r, column=1, columnspan=3, sticky="nsew", **pad)
        root.rowconfigure(r, weight=3)
        caja.columnconfigure(0, weight=1)
        caja.rowconfigure(0, weight=1)
        self.t_desc = tk.Text(caja, height=7, wrap="word", undo=True,
                              font=("Segoe UI", 9), relief="solid", borderwidth=1)
        self.t_desc.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(caja, command=self.t_desc.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.t_desc.configure(yscrollcommand=sb.set)
        self.t_desc.bind("<KeyRelease>", self._refresh_title)
        r += 1

        ttk.Label(root, text="Imagenes").grid(row=r, column=0, sticky="nw", **pad)
        att = ttk.Frame(root)
        att.grid(row=r, column=1, columnspan=3, sticky="nsew", **pad)
        root.rowconfigure(r, weight=1)
        att.columnconfigure(0, weight=1)
        att.rowconfigure(0, weight=1)
        self.lb_att = tk.Listbox(att, height=3, selectmode="extended")
        self.lb_att.grid(row=0, column=0, sticky="nsew")
        sb2 = ttk.Scrollbar(att, command=self.lb_att.yview)
        sb2.grid(row=0, column=1, sticky="ns")
        self.lb_att.configure(yscrollcommand=sb2.set)
        botones = ttk.Frame(att)
        botones.grid(row=0, column=2, sticky="ns", padx=(6, 0))
        ttk.Button(botones, text="Agregar...", width=12,
                   command=self.add_files).pack(fill="x", pady=1)
        ttk.Button(botones, text="Pegar Ctrl+V", width=12,
                   command=self.paste_clipboard).pack(fill="x", pady=1)
        ttk.Button(botones, text="Quitar", width=12,
                   command=self.remove_selected).pack(fill="x", pady=1)
        self.bind("<Control-v>", self._on_ctrl_v)
        self.bind("<Control-V>", self._on_ctrl_v)
        r += 1

        pie = ttk.Frame(root)
        pie.grid(row=r, column=0, columnspan=4, sticky="ew", padx=8, pady=(8, 0))
        self.lbl_status = ttk.Label(pie, text="", foreground="#0a6", wraplength=460)
        self.lbl_status.pack(side="left", fill="x", expand=True)
        self.btn_send = ttk.Button(pie, text="Enviar a GLPI", command=self.submit)
        self.btn_send.pack(side="right")
        ttk.Button(pie, text="Cancelar", command=self.cancel).pack(side="right", padx=6)
    def _prefill(self):
        num = pretty_number(self.call.get("number"))
        self.e_telefono.insert(0, num)

        norm = normalize_number(num, self.cfg)
        mapa = self.cfg["ui"].get("telefono_sucursal", {}) or {}
        suc = mapa.get(num) or (mapa.get(norm) if norm else None)
        if not suc and norm:
            for k, v in mapa.items():
                if normalize_number(k, self.cfg) == norm:
                    suc = v
                    break
        if suc:
            self.cb_sucursal.set(suc)

        if self.call.get("name"):
            self.e_nombre.insert(0, self.call["name"])

        urg = self.cfg["ui"].get("urgencias", {}) or {}
        default_urg = int(self.cfg["glpi"].get("urgency", 3))
        for label, val in urg.items():
            if int(val) == default_urg:
                self.cb_urgencia.set(label)
                break
        if not self.cb_urgencia.get() and urg:
            self.cb_urgencia.set(list(urg.keys())[0])

        # Entidad: la que indique glpi.entities_id, o la primera de la lista.
        ents = self.cfg["ui"].get("entidades", {}) or {}
        if self.cb_entidad is not None and ents:
            deseada = int(self.cfg["glpi"].get("entities_id", 0))
            elegida = next((n for n, v in ents.items() if int(v) == deseada), None)
            self.cb_entidad.set(elegida or list(ents.keys())[0])
        self._on_entidad()
        self._on_correo()

        self._refresh_title()
        (self.cb_sucursal if suc is None else self.e_nombre).focus_set()

    def _focus_me(self):
        try:
            self.lift()
            self.focus_force()
            self.bell()
        except Exception:
            pass

    # -- correo del solicitante -------------------------------------------- #
    def resolver_correo(self, correo):
        """Busca el correo entre los usuarios de GLPI.

        Devuelve (users_id, nombre). Primero en el mapa que dejo --autoconfig
        (sin red); si no esta, consulta la API por si el usuario es nuevo.
        """
        correo = (correo or "").strip().lower()
        if not correo:
            return 0, ""
        mapa = self.cfg["ui"].get("correos_usuarios", {}) or {}
        dato = mapa.get(correo)
        if not dato:
            for k, v in mapa.items():
                if k.strip().lower() == correo:
                    dato = v
                    break
        if dato:
            return int(dato.get("id") or 0), dato.get("nombre") or ""
        try:
            return buscar_usuario_por_correo(self.cfg, correo)
        except Exception as exc:
            log("No se pudo consultar el correo '%s' en GLPI: %s" % (correo, exc))
            return 0, ""

    def correo_actual(self):
        """Une el usuario escrito con el dominio elegido.

        Si en el campo de usuario se pega un correo completo, se respeta tal cual
        y el dominio del desplegable se ignora.
        """
        usuario = self.e_usuario.get().strip()
        if not usuario:
            return ""
        if "@" in usuario:
            return usuario.lstrip("@") if usuario.startswith("@") else usuario
        return usuario + self.cb_dominio.get().strip()

    def _otro_dominio(self, usuario):
        """Busca el mismo usuario en los demas dominios conocidos."""
        mapa = self.cfg["ui"].get("correos_usuarios", {}) or {}
        actual = self.cb_dominio.get().strip().lower()
        for dom in (self.cfg["ui"].get("dominios_correo") or []):
            if dom.lower() == actual:
                continue
            candidato = (usuario + dom).lower()
            if candidato in mapa:
                return candidato, mapa[candidato].get("nombre") or ""
        return "", ""

    def _on_dominio(self, event=None):
        self.dominio_manual = True
        self._on_correo()

    def _dominio_efectivo(self):
        """Dominio que manda para elegir la entidad.

        Si en el campo de usuario hay un correo completo, gana ese dominio; si
        no, el del desplegable. Con el formulario recien abierto (sin usuario y
        sin haber tocado el desplegable) devuelve "" para no mover la entidad
        que viene de glpi.entities_id.
        """
        usuario = self.e_usuario.get().strip()
        if "@" in usuario:
            return "@" + usuario.rsplit("@", 1)[1].strip().lower()
        if usuario or self.dominio_manual:
            return (self.cb_dominio.get() or "").strip().lower()
        return ""

    def _entidad_por_dominio(self, dominio):
        """Nombre de entidad mapeado a ese dominio en ui.entidad_por_dominio."""
        if not dominio:
            return ""
        mapa = self.cfg["ui"].get("entidad_por_dominio", {}) or {}
        for dom, nombre in mapa.items():
            if dom.strip().lower() == dominio:
                return nombre
        return ""

    def _auto_entidad_por_correo(self):
        """Cambia la entidad segun el dominio del correo elegido."""
        if self.cb_entidad is None or self._ajustando:
            return
        ents = self.cfg["ui"].get("entidades", {}) or {}
        nombre = self._entidad_por_dominio(self._dominio_efectivo())
        if nombre and nombre in ents and self.cb_entidad.get() != nombre:
            self._ajustando = True          # evita rebotes entidad <-> dominio
            try:
                self.cb_entidad.set(nombre)
                self._on_entidad()
            finally:
                self._ajustando = False

    def _on_categoria_manual(self, event=None):
        self.categoria_manual = True

    def _on_correo(self, event=None):
        self._auto_entidad_por_correo()
        usuario = self.e_usuario.get().strip()
        if not usuario:
            self.lbl_correo.configure(text="solicitante por defecto", foreground="#888")
            return
        correo = self.correo_actual()
        uid, nombre = self.resolver_correo(correo)
        if uid:
            self.lbl_correo.configure(text="-> %s  (id %s)" % (nombre or "usuario", uid),
                                      foreground="#0a6")
            return
        if "@" not in usuario:
            otro, nom_otro = self._otro_dominio(usuario)
            if otro:
                self.lbl_correo.configure(
                    text="no existe; si esta como %s (%s)" % (otro, nom_otro),
                    foreground="#c60")
                return
        self.lbl_correo.configure(
            text="sin usuario en GLPI: ira el correo como solicitante", foreground="#c60")

    # -- entidad ----------------------------------------------------------- #
    def entidad_id(self):
        ents = self.cfg["ui"].get("entidades", {}) or {}
        if self.cb_entidad is not None and self.cb_entidad.get() in ents:
            return int(ents[self.cb_entidad.get()])
        return int(self.cfg["glpi"].get("entities_id", 0))

    def _visibles(self, clave):
        """Categorias o sucursales visibles en la entidad elegida."""
        por = self.cfg["ui"].get("por_entidad", {}) or {}
        bloque = por.get(str(self.entidad_id())) or {}
        return bloque.get(clave) or {}

    def _categoria_por_defecto(self, valores):
        """Categoria preferida (ui.categoria_preferida, ej. 'MDA') si esta
        visible entre las categorias de la entidad elegida; si no, ninguna."""
        preferida = (self.cfg["ui"].get("categoria_preferida") or "").strip().lower()
        if preferida:
            for v in valores:
                if preferida in v.lower():
                    return v
        return ""

    def _on_entidad(self, event=None):
        """Reajusta categoria y sucursal a lo que existe en la entidad elegida."""
        # La categoria debe ser un id valido en la entidad: si no hay ninguna
        # visible, el desplegable queda vacio (no se ofrecen ids invalidos).
        cats = self._visibles("categorias")
        if self.cb_categoria is not None:
            tiene_mapa = bool(self.cfg["ui"].get("por_entidad") or {})
            valores = (sorted(cats.keys()) if tiene_mapa
                       else list((self.cfg["ui"].get("categorias") or {}).keys()))
            actual = self.cb_categoria.get()
            self.cb_categoria.configure(values=[""] + valores,
                                        state="readonly" if valores else "disabled")
            if actual in valores:
                self.cb_categoria.set(actual)
            elif not self.categoria_manual:
                self.cb_categoria.set(self._categoria_por_defecto(valores))
            else:
                self.cb_categoria.set("")

        sucs = self._visibles("sucursales")
        self.sucursal_texto_libre = not bool(sucs)
        valores = sorted(sucs.keys()) if sucs else list(self.cfg["ui"].get("sucursales", []))
        actual = self.cb_sucursal.get()
        self.cb_sucursal.configure(values=valores)
        if actual:
            self.cb_sucursal.set(actual)

        # El dominio del correo sigue a la entidad, salvo que se haya elegido a
        # mano. Solo se reavisa si el dominio realmente cambio, para no rebotar
        # con _auto_entidad_por_correo() (que hace el camino inverso).
        porent = self.cfg["ui"].get("dominio_por_entidad") or {}
        sugerido = porent.get(str(self.entidad_id()))
        if (sugerido and not getattr(self, "dominio_manual", False)
                and getattr(self, "cb_dominio", None) is not None
                and sugerido != self.cb_dominio.get()
                and sugerido in (self.cb_dominio["values"] or ())):
            self.cb_dominio.set(sugerido)
            self._on_correo()

        if self.lbl_entidad is not None:
            if not cats and not sucs:
                aviso = "sin categorias ni ubicaciones propias"
            elif not sucs:
                aviso = "%d categorias; la sucursal ira solo como texto" % len(cats)
            else:
                aviso = "%d categorias, %d ubicaciones" % (len(cats), len(sucs))
            self.lbl_entidad.configure(text=aviso)

    # -- titulo ------------------------------------------------------------ #
    _MODIFIERS = ("Tab", "Shift_L", "Shift_R", "Control_L", "Control_R",
                  "Alt_L", "Alt_R", "Left", "Right", "Up", "Down", "Home", "End")

    def _title_touched(self, event=None):
        if event is not None and event.keysym in self._MODIFIERS:
            return
        self.title_manual = True

    def _force_title(self):
        self.title_manual = False
        self.dominio_manual = False
        self._refresh_title()

    def _resumen_desc(self, desc):
        """Elige la primera oracion con contenido real (sin saludos ni frases
        de relleno como 'el cliente indica que...') y la recorta a resumen_max."""
        limit = int(self.cfg["ui"].get("resumen_max", 70))
        elegido, primera = "", ""
        for frag in fragmentos_descripcion(desc):
            if not primera:
                primera = frag
            limpio = limpiar_resumen(self.cfg, frag)
            if len(limpio) >= 3:
                elegido = limpio
                break
        resumen = elegido or primera
        if not resumen:
            return ""
        resumen = resumen[:1].upper() + resumen[1:]
        if len(resumen) <= limit:
            return resumen
        cut = resumen[:limit]
        return (cut.rsplit(" ", 1)[0] if " " in cut else cut) + "..."

    def _build_title(self):
        suc = self.cb_sucursal.get().strip() or "Sin sucursal"
        desc = self.t_desc.get("1.0", "end").strip()
        resumen = self._resumen_desc(desc) if desc else ""
        tpl = self.cfg["ui"].get("title_template", "{sucursal} - {resumen}")
        try:
            out = tpl.format(sucursal=suc, resumen=resumen,
                             telefono=self.e_telefono.get().strip(),
                             nombre=self.e_nombre.get().strip())
        except Exception:
            out = "%s - %s" % (suc, resumen)
        return out.strip().rstrip("-").strip()

    def _refresh_title(self, event=None):
        if self.title_manual:
            return
        val = self._build_title()
        if self.e_titulo.get() != val:
            self.e_titulo.delete(0, "end")
            self.e_titulo.insert(0, val)

    # -- adjuntos ---------------------------------------------------------- #
    def _tmp(self):
        if not self._tmpdir:
            self._tmpdir = tempfile.mkdtemp(prefix="xcally_glpi_")
        return self._tmpdir

    def add_files(self):
        paths = filedialog.askopenfilenames(
            parent=self,
            title="Seleccionar imagenes o archivos",
            filetypes=[("Imagenes", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"),
                       ("Documentos", "*.pdf *.txt *.log *.csv *.xlsx *.docx"),
                       ("Todos los archivos", "*.*")])
        for p in paths:
            self._add(p)

    def _add(self, path):
        if path and path not in self.attachments:
            self.attachments.append(path)
            self.lb_att.insert("end", os.path.basename(path))

    def remove_selected(self):
        for idx in sorted(self.lb_att.curselection(), reverse=True):
            self.lb_att.delete(idx)
            del self.attachments[idx]

    def _on_ctrl_v(self, event=None):
        # dentro del cuadro de descripcion Ctrl+V pega texto normalmente
        if event is not None and event.widget is self.t_desc:
            return None
        self.paste_clipboard()
        return "break"

    def paste_clipboard(self):
        # 1) rutas de archivo copiadas desde el Explorador
        try:
            data = self.clipboard_get()
        except Exception:
            data = ""
        added = False
        for line in (data or "").splitlines():
            cand = line.strip().strip('"')
            if cand and os.path.isfile(cand):
                self._add(cand)
                added = True
        if added:
            self.set_status("Adjunto(s) agregados desde el portapapeles.")
            return
        # 2) imagen (captura de pantalla) en el portapapeles -> requiere Pillow
        if not PIL_OK:
            self.set_status("Para pegar capturas instala Pillow:  pip install pillow", err=True)
            return
        try:
            img = ImageGrab.grabclipboard()
        except Exception as exc:
            self.set_status("No se pudo leer el portapapeles: %s" % exc, err=True)
            return
        if isinstance(img, list):
            for c in img:
                if os.path.isfile(c):
                    self._add(c)
            self.set_status("Adjunto(s) agregados desde el portapapeles.")
            return
        if img is None:
            self.set_status("El portapapeles no contiene imagen ni archivos.", err=True)
            return
        fn = os.path.join(self._tmp(), "captura_%s.png" % datetime.now().strftime("%H%M%S_%f"))
        img.save(fn, "PNG")
        self._add(fn)
        self.set_status("Captura pegada como %s" % os.path.basename(fn))

    # -- envio ------------------------------------------------------------- #
    def set_status(self, msg, err=False):
        if self.closed:
            return
        try:
            self.lbl_status.configure(text=msg, foreground="#c00" if err else "#0a6")
        except Exception:
            pass

    def _collect(self):
        nombre = self.e_nombre.get().strip()
        sucursal = self.cb_sucursal.get().strip()
        telefono = self.e_telefono.get().strip()
        titulo = self.e_titulo.get().strip()
        desc = self.t_desc.get("1.0", "end").strip()

        faltan = [n for n, v in (("Nombre", nombre), ("Sucursal", sucursal),
                                 ("Telefono", telefono), ("Titulo", titulo),
                                 ("Descripcion", desc)) if not v]
        if faltan:
            raise ValueError("Completa los campos obligatorios: " + ", ".join(faltan))

        correo = self.correo_actual()

        # La cola y el uniqueid de xCally no se incluyen en el ticket; el
        # uniqueid se sigue usando internamente para no reabrir la ventana.
        partes = ["<p><b>Sucursal:</b> %s<br>" % esc(sucursal),
                  "<b>Nombre:</b> %s<br>" % esc(nombre),
                  "<b>Telefono:</b> %s<br>" % esc(telefono)]
        if correo:
            partes.append("<b>Correo:</b> %s<br>" % esc(correo))
        partes.append("<b>Fecha llamada:</b> %s" % esc(self.call.get("ts_str", "")))
        partes.append("</p><hr><p>%s</p>" % esc(desc).replace("\n", "<br>"))
        contenido = "".join(partes)

        g = self.cfg["glpi"]
        fields = {
            "name": titulo,
            "content": contenido,
            "type": int(g.get("type", 1)),
            "requesttypes_id": int(g.get("requesttypes_id", 1)),
            "entities_id": self.entidad_id(),
        }
        urg_map = self.cfg["ui"].get("urgencias", {}) or {}
        sel = self.cb_urgencia.get()
        fields["urgency"] = int(urg_map[sel]) if sel in urg_map else int(g.get("urgency", 3))

        # Categoria: solo si es valida en la entidad elegida.
        tiene_mapa = bool(self.cfg["ui"].get("por_entidad") or {})
        visibles = self._visibles("categorias")
        fuente = visibles if tiene_mapa else (self.cfg["ui"].get("categorias") or {})
        cat_id = 0
        if self.cb_categoria is not None and self.cb_categoria.get():
            cat_id = int(fuente.get(self.cb_categoria.get()) or 0)
        if not cat_id:
            defecto = int(g.get("itilcategories_id", g.get("itilcategory_id", 0)) or 0)
            if defecto and (not tiene_mapa or defecto in fuente.values()):
                cat_id = defecto
        if cat_id:
            # El campo en la tabla de GLPI es 'itilcategories_id' (en plural);
            # 'itilcategory_id' se acepta sin error pero se ignora.
            fields["itilcategories_id"] = cat_id

        # Solicitante: siempre el correo armado (usuario + dominio).
        #   - si ese correo es de un usuario de GLPI  -> ese usuario
        #   - si no existe como usuario               -> el correo tal cual,
        #     como direccion alternativa del solicitante (users_id 0)
        #   - si el campo va vacio                    -> el de la configuracion
        if correo:
            uid, _ = self.resolver_correo(correo)
            if uid:
                fields["_users_id_requester"] = uid
            else:
                fields["_users_id_requester"] = 0
                fields["_users_id_requester_notif"] = {
                    "use_notification": [1],
                    "alternative_email": [correo],
                }
        elif int(g.get("users_id_requester", 0) or 0):
            fields["_users_id_requester"] = int(g["users_id_requester"])
        # Tecnico asignado automaticamente.
        if int(g.get("users_id_assign", 0) or 0):
            fields["_users_id_assign"] = int(g["users_id_assign"])
        if int(g.get("groups_id_assign", 0) or 0):
            fields["_groups_id_assign"] = int(g["groups_id_assign"])

        # locations_id solo si esa ubicacion existe y es visible en la entidad
        # elegida. Si no, la sucursal viaja como texto dentro de la descripcion.
        locs = self._visibles("sucursales")
        if not locs and not (self.cfg["ui"].get("por_entidad") or {}):
            locs = self.cfg["ui"].get("sucursales_locations") or {}
        loc_id = locs.get(sucursal)
        if loc_id is None:
            for nombre, lid in locs.items():
                if nombre.strip().lower() == sucursal.lower():
                    loc_id = lid
                    break
        if loc_id:
            fields["locations_id"] = int(loc_id)
        return fields

    def submit(self):
        if self.sending:
            return
        try:
            fields = self._collect()
        except ValueError as exc:
            messagebox.showwarning(APP_NAME, str(exc), parent=self)
            return
        self.sending = True
        self.btn_send.configure(state="disabled", text="Enviando...")
        attachments = list(self.attachments)
        cfg = self.cfg

        def worker():
            try:
                tid, errs = submit_to_glpi(
                    cfg, fields, attachments,
                    progress=lambda m: self._ui(self.set_status, m))
                self._ui(self._done_ok, tid, errs)
            except Exception as exc:
                log("ERROR enviando a GLPI:\n" + traceback.format_exc())
                path = save_pending(cfg, fields, attachments)
                self._ui(self._done_err, str(exc), path)

        threading.Thread(target=worker, daemon=True).start()

    def _ui(self, fn, *a):
        if self.closed:
            return
        try:
            self.after(0, fn, *a)
        except Exception:
            pass

    def _done_ok(self, tid, errs):
        msg = "Ticket #%s creado correctamente en GLPI." % tid
        if errs:
            msg += "\n\nAdjuntos con problemas:\n- " + "\n- ".join(errs)
        messagebox.showinfo(APP_NAME, msg, parent=self)
        self.close()

    def _done_err(self, err, pending_path):
        self.sending = False
        self.btn_send.configure(state="normal", text="Enviar a GLPI")
        self.set_status("Fallo el envio.", err=True)
        extra = ""
        if pending_path:
            extra = ("\n\nSe guardo el borrador en:\n%s\n\nReintenta luego con:\n"
                     "python xcally_glpi.py --retry-pending" % pending_path)
        messagebox.showerror(APP_NAME, "No se pudo crear el ticket:\n\n%s%s" % (err, extra),
                             parent=self)

    # -- cierre ------------------------------------------------------------ #
    def cancel(self):
        if self.sending:
            if not messagebox.askyesno(APP_NAME, "Hay un envio en curso. Cerrar de todos modos?",
                                       parent=self):
                return
        elif self.t_desc.get("1.0", "end").strip():
            if not messagebox.askyesno(APP_NAME, "Descartar el registro de esta llamada?",
                                       parent=self):
                return
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.destroy()
        finally:
            try:
                self.on_close_cb(self.call)
            except Exception:
                log("ERROR en callback de cierre:\n" + traceback.format_exc())


# --------------------------------------------------------------------------- #
# Deteccion de llamadas
# --------------------------------------------------------------------------- #
class CallDispatcher:
    """Cola de eventos + deduplicacion: una sola ventana por llamada."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.seen = {}          # clave -> timestamp del ultimo aviso
        self.open_windows = 0

    def _key(self, number, call_id):
        if call_id:
            return "id:%s" % call_id
        return "num:%s" % normalize_number(number, self.cfg)

    def offer(self, number, call_id=None, name=None, queue_name=None, source="http"):
        """Encola la apertura de la ventana. Devuelve (aceptada, motivo)."""
        now = time.time()
        ttl = int(self.cfg["call"].get("dedup_seconds", 300))
        key = self._key(number, call_id)
        with self.lock:
            for k in [k for k, t in self.seen.items() if now - t > ttl]:
                self.seen.pop(k, None)
            if key in self.seen:
                self.seen[key] = now          # refresca: sigue siendo la misma llamada
                return False, "duplicada (misma llamada en curso)"
            if self.cfg["call"].get("single_window", True) and self.open_windows > 0:
                return False, "ya hay una ventana abierta"
            self.seen[key] = now
            self.open_windows += 1

        digits = normalize_number(number, self.cfg)
        min_d = int(self.cfg["call"].get("min_digits", 6))
        if digits and len(digits) < min_d:
            log("Aviso: numero corto '%s' (anexo interno?)" % number)

        call = {"number": pretty_number(number),
                "call_id": call_id or "",
                "name": name or "",
                "queue": queue_name or "",
                "ts": now,
                "ts_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source": source}
        self.q.put(call)
        log("Llamada aceptada: %s (id=%s, via %s)"
            % (call["number"], call["call_id"] or "-", source))
        return True, "ok"

    def released(self, call):
        with self.lock:
            self.open_windows = max(0, self.open_windows - 1)
            # la clave se mantiene en 'seen' para no reabrir durante la misma llamada
        log("Ventana cerrada para %s" % call.get("number"))

    def clear(self, number=None, call_id=None):
        """Hangup: libera la clave para que la proxima llamada si abra ventana."""
        with self.lock:
            if call_id:
                self.seen.pop("id:%s" % call_id, None)
            if number:
                self.seen.pop("num:%s" % normalize_number(number, self.cfg), None)


class CallHTTPHandler(BaseHTTPRequestHandler):
    server_version = "xCallyGlpiPopup/1.0"
    dispatcher = None
    cfg = None

    def log_message(self, fmt, *args):
        pass  # silencia el log de acceso por defecto

    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _auth_ok(self, params):
        want = (self.cfg["listener"].get("token") or "").strip()
        if not want:
            return True
        got = self.headers.get("X-Auth-Token") or ""
        if not got and params:
            got = (params.get("token") or [""])[0]
        return got == want

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        self._handle(u.path, urllib.parse.parse_qs(u.query))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(u.query)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        except Exception:
            raw = ""
        if raw:
            ctype = (self.headers.get("Content-Type") or "").lower()
            try:
                if "json" in ctype:
                    for k, v in (json.loads(raw) or {}).items():
                        params.setdefault(k, [str(v)])
                else:
                    for k, v in urllib.parse.parse_qs(raw).items():
                        params.setdefault(k, v)
            except Exception:
                pass
        self._handle(u.path, params)

    def _handle(self, path, params):
        path = path.rstrip("/") or "/"

        if path in ("/", "/health"):
            return self._reply(200, {"status": "ok", "app": "xcally-glpi-popup"})

        if not self._auth_ok(params):
            return self._reply(401, {"error": "token invalido"})

        def first(*names):
            for n in names:
                for val in params.get(n, []):
                    val = str(val).strip()
                    if val and val.lower() not in ("none", "null", "undefined"):
                        return val
            return ""

        if path in ("/incoming", "/call", "/llamada", "/ring"):
            number = first("number", "numero", "phone", "telefono", "calleridnum",
                           "callerid", "from", "source", "cid")
            call_id = first("id", "uniqueid", "callid", "linkedid")
            name = first("name", "nombre", "calleridname")
            qn = first("queue", "cola")
            if not number:
                return self._reply(400, {"error": "falta el parametro 'number'"})
            ok, why = self.dispatcher.offer(number, call_id, name, qn, source="http")
            return self._reply(200, {"opened": ok, "reason": why, "number": number})

        if path in ("/hangup", "/end", "/fin"):
            number = first("number", "numero", "phone", "telefono")
            call_id = first("id", "uniqueid", "callid", "linkedid")
            self.dispatcher.clear(number, call_id)
            return self._reply(200, {"cleared": True})

        return self._reply(404, {"error": "ruta no encontrada"})


def start_listener(cfg, dispatcher):
    CallHTTPHandler.dispatcher = dispatcher
    CallHTTPHandler.cfg = cfg
    host = cfg["listener"].get("host", "127.0.0.1")
    port = int(cfg["listener"].get("port", 8765))
    srv = ThreadingHTTPServer((host, port), CallHTTPHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("Escuchando llamadas en http://%s:%s/incoming?number=..." % (host, port))
    return srv


# --------------------------------------------------------------------------- #
# Deteccion en el escritorio: log de la PhoneBar de xCally
# --------------------------------------------------------------------------- #
# La PhoneBar (C:\Program Files (x86)\Xenialab s.r.l\XCALLY\PhoneBar.exe) escribe
# con log4net en %USERPROFILE%\xCALLY\Logs\phonebar.log a nivel DEBUG. Ahi vuelca
# dos eventos con el JSON completo de la llamada:
#
#   OnQueueCall()             -> "ring"    : la llamada entra a la cola y suena.
#                                Trae calleridnum, queue, uniqueid.
#   OnWebBrowserIntegration() -> "connect" : el agente contesta.
#                                Trae ademas membername, type, calleridname...
#
# No requiere configurar nada en el servidor xCally.

PHONEBAR_DEFAULT_LOG = os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")), "xCALLY", "Logs", "phonebar.log")

_LOGLINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \| ")

_PHONEBAR_MARKERS = (
    ("ring", "OnQueueCall() - Queue Call Response:"),
    ("connect", "OnWebBrowserIntegration() - WebBrowser Integration Response:"),
)

_MAX_BLOCK_CHARS = 200000


class PhoneBarLogParser:
    """Extrae los bloques JSON de eventos de llamada del log de la PhoneBar.

    El log es multilinea (JSON con formato), asi que se acumula hasta que las
    llaves se equilibran, ignorando llaves dentro de cadenas.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.kind = None
        self.text = ""
        self.depth = 0
        self.in_str = False
        self.escaped = False

    def feed_line(self, line):
        """Devuelve la lista de eventos completos hallados en esta linea."""
        if self.kind is None:
            for kind, marker in _PHONEBAR_MARKERS:
                i = line.find(marker)
                if i < 0:
                    continue
                j = line.find("{", i + len(marker))
                if j < 0:
                    break
                self.reset()
                self.kind = kind
                return [e for e in (self._consume(line[j:]),) if e]
            return []

        # Si arranca otro mensaje de log, el bloque quedo incompleto: descartar.
        if _LOGLINE_RE.match(line):
            self.reset()
            return self.feed_line(line)
        if len(self.text) > _MAX_BLOCK_CHARS:
            self.reset()
            return []
        return [e for e in (self._consume(line),) if e]

    def _consume(self, chunk):
        start = len(self.text)
        self.text = (self.text + "\n" + chunk) if self.text else chunk
        i = start if not start else start + 1
        while i < len(self.text):
            ch = self.text[i]
            if self.in_str:
                if self.escaped:
                    self.escaped = False
                elif ch == "\\":
                    self.escaped = True
                elif ch == '"':
                    self.in_str = False
            elif ch == '"':
                self.in_str = True
            elif ch == "{":
                self.depth += 1
            elif ch == "}":
                self.depth -= 1
                if self.depth <= 0:
                    payload, kind = self.text[:i + 1], self.kind
                    self.reset()
                    try:
                        return {"kind": kind, "data": json.loads(payload)}
                    except Exception:
                        return None
            i += 1
        return None


def phonebar_extract_call(event):
    """Normaliza un evento del log a un dict simple, o None si no sirve."""
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return None
    number = str(data.get("calleridnum") or "").strip()
    if not number:
        return None
    name = str(data.get("calleridname") or "").strip()
    if name == number or not re.search(r"[A-Za-z]", name):
        name = ""          # calleridname suele venir igual al numero
    return {
        "kind": event.get("kind"),
        "number": number,
        "uniqueid": str(data.get("uniqueid") or "").strip(),
        "queue": str(data.get("queue") or "").strip(),
        "member": str(data.get("membername") or "").strip(),
        "type": str(data.get("type") or ("inbound" if event.get("kind") == "ring" else "")).strip(),
        "name": name,
    }


def phonebar_accepts(cfg, call):
    """Aplica los filtros de config. Devuelve (ok, motivo_del_descarte)."""
    p = cfg.get("phonebar", {}) or {}
    modo = (p.get("evento_apertura") or "connect").lower()
    if modo in ("ambos", "both", "todos"):
        pass
    elif modo != call["kind"]:
        return False, "evento '%s' descartado (evento_apertura=%s)" % (call["kind"], modo)

    if p.get("solo_entrantes", True) and call["type"] and call["type"] != "inbound":
        return False, "no es entrante (type=%s)" % call["type"]

    agente = (p.get("agente") or "").strip()
    if agente and call["member"] and call["member"].lower() != agente.lower():
        return False, "otro agente (%s)" % call["member"]

    colas = [str(c).strip().lower() for c in (p.get("colas") or []) if str(c).strip()]
    if colas and call["queue"] and call["queue"].lower() not in colas:
        return False, "cola no incluida (%s)" % call["queue"]

    return True, ""


class PhoneBarLogWatcher(threading.Thread):
    """Sigue el log de la PhoneBar y avisa de cada llamada entrante."""

    def __init__(self, cfg, dispatcher, from_start=False, report=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.d = dispatcher
        self.from_start = from_start
        self.report = report or (lambda m: None)
        self.stop_flag = threading.Event()

    def log_path(self):
        return (self.cfg["phonebar"].get("log_file") or "").strip() or PHONEBAR_DEFAULT_LOG

    def run(self):
        path = self.log_path()
        every = max(0.2, float(self.cfg["phonebar"].get("poll_seconds", 1.0)))
        log("Vigilando el log de la PhoneBar: %s" % path)

        parser = PhoneBarLogParser()
        remainder = b""
        pos = None

        while not self.stop_flag.is_set():
            try:
                size = os.path.getsize(path)
                if pos is None:
                    pos = 0 if self.from_start else size
                    if not self.from_start:
                        log("Log posicionado al final (%d bytes). Esperando llamadas..." % size)
                elif size < pos:
                    log("Rotacion del log detectada (%d -> %d bytes). Reiniciando lectura."
                        % (pos, size))
                    pos, remainder = 0, b""
                    parser.reset()

                if size > pos:
                    with open(path, "rb") as fh:
                        fh.seek(pos)
                        chunk = fh.read()
                        pos = fh.tell()
                    buf = remainder + chunk
                    lines = buf.split(b"\n")
                    remainder = lines.pop()        # ultima linea posiblemente incompleta
                    for raw in lines:
                        text = raw.decode("utf-8", "replace").rstrip("\r")
                        for ev in parser.feed_line(text):
                            self._handle(ev)
            except FileNotFoundError:
                log("Aun no existe el log de la PhoneBar (%s). Reintentando..." % path)
                pos = None
                time.sleep(5)
                continue
            except Exception as exc:
                log("Error leyendo el log de la PhoneBar: %s" % exc)
            self.stop_flag.wait(every)

    def _handle(self, event):
        call = phonebar_extract_call(event)
        if not call:
            return
        ok, why = phonebar_accepts(self.cfg, call)
        etiqueta = "%s %s (uid=%s, cola=%s, agente=%s)" % (
            call["kind"], call["number"], call["uniqueid"] or "-",
            call["queue"] or "-", call["member"] or "-")
        if not ok:
            log("PhoneBar: %s -> ignorado: %s" % (etiqueta, why))
            self.report({"call": call, "accepted": False, "reason": why})
            return
        if self.d is None:
            log("PhoneBar: %s -> detectada (modo espia, no se abre nada)" % etiqueta)
            self.report({"call": call, "accepted": True, "reason": ""})
            return
        abierta, motivo = self.d.offer(call["number"], call["uniqueid"], call["name"],
                                       call["queue"], source="phonebar-log:%s" % call["kind"])
        log("PhoneBar: %s -> %s" % (etiqueta,
                                    "abriendo ventana" if abierta else "sin abrir: " + motivo))
        self.report({"call": call, "accepted": abierta, "reason": motivo})


class XCallyPoller(threading.Thread):
    """Opcional: consulta la API de xCally buscando llamadas activas del agente."""

    def __init__(self, cfg, dispatcher):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.d = dispatcher

    def run(self):
        x = self.cfg["xcally"]
        base = (x.get("base_url") or "").rstrip("/")
        if not base or requests is None:
            log("Poller xCally desactivado (falta base_url o la libreria requests).")
            return
        url = base + x.get("poll_endpoint", "/api/voice/channels")
        headers = {"Accept": "application/json"}
        tok = (x.get("token") or "").strip()
        if tok:
            headers["Authorization"] = tok if tok.lower().startswith("bearer") else "Bearer %s" % tok
        every = max(1, int(x.get("poll_seconds", 3)))
        log("Poller xCally activo: %s cada %ss" % (url, every))
        while True:
            try:
                r = requests.get(url, headers=headers, timeout=10,
                                 verify=bool(x.get("verify_ssl", True)))
                if r.status_code == 200:
                    for row in self._rows(r.json()):
                        self._maybe(row)
                else:
                    log("Poller xCally HTTP %s: %s" % (r.status_code, r.text[:200]))
            except Exception as exc:
                log("Poller xCally error: %s" % exc)
            time.sleep(every)

    @staticmethod
    def _rows(data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("rows", "data", "results", "channels", "calls"):
                if isinstance(data.get(key), list):
                    return data[key]
            return [data]
        return []

    def _maybe(self, row):
        if not isinstance(row, dict):
            return
        x = self.cfg["xcally"]
        agent = (x.get("agent_filter") or "").strip()
        if agent and agent not in json.dumps(row, default=str):
            return
        number = ""
        for f in x.get("number_fields", []):
            if row.get(f):
                number = str(row[f])
                break
        call_id = ""
        for f in x.get("id_fields", []):
            if row.get(f):
                call_id = str(row[f])
                break
        if number:
            self.d.offer(number, call_id, source="xcally-api")


# --------------------------------------------------------------------------- #
# Aplicacion
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dispatcher = CallDispatcher(cfg)
        self.root = tk.Tk()
        self.root.withdraw()          # residente, sin ventana principal visible
        self.root.title(APP_NAME)
        self.root.after(300, self._pump)

    def _pump(self):
        try:
            while True:
                call = self.dispatcher.q.get_nowait()
                TicketWindow(self.root, self.cfg, call, self.dispatcher.released)
        except queue.Empty:
            pass
        except Exception:
            log("ERROR abriendo la ventana:\n" + traceback.format_exc())
        self.root.after(300, self._pump)

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def cmd_ping(cfg, number):
    host = cfg["listener"].get("host", "127.0.0.1")
    port = int(cfg["listener"].get("port", 8765))
    q = {"number": number, "id": "test-%d" % int(time.time())}
    if cfg["listener"].get("token"):
        q["token"] = cfg["listener"]["token"]
    url = "http://%s:%s/incoming?%s" % (host, port, urllib.parse.urlencode(q))
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            print(r.read().decode("utf-8"))
    except Exception as exc:
        print("No se pudo contactar el listener (%s): %s" % (url, exc))
        return 1
    return 0


def cmd_check_glpi(cfg):
    try:
        c = GlpiClient(cfg)
        c.init_session()
        print("OK - sesion GLPI iniciada (session_token %s...)" % c.session_token[:8])
        c.kill_session()
        return 0
    except Exception as exc:
        print("ERROR:", exc)
        return 1


def cmd_probar_log(cfg, arg):
    """Recorre los log historicos de la PhoneBar y muestra las llamadas detectadas."""
    p = (cfg["phonebar"].get("log_file") or "").strip() or PHONEBAR_DEFAULT_LOG
    carpeta = os.path.dirname(p)
    base = os.path.basename(p)
    if arg and arg not in ("*", "todos"):
        archivos = [arg if os.path.isabs(arg) else os.path.join(carpeta, arg)]
    else:
        try:
            archivos = sorted(os.path.join(carpeta, f) for f in os.listdir(carpeta)
                              if f.startswith(base))
        except Exception as exc:
            print("No se pudo leer %s: %s" % (carpeta, exc))
            return 1
    if not archivos:
        print("No se encontraron archivos de log en %s" % carpeta)
        return 1

    total = aceptadas = 0
    print("Analizando %d archivo(s) de log...\n" % len(archivos))
    print("%-8s %-14s %-11s %-22s %-14s %s"
          % ("EVENTO", "NUMERO", "COLA", "UNIQUEID", "AGENTE", "SE ABRIRIA?"))
    print("-" * 96)
    for ruta in archivos:
        parser = PhoneBarLogParser()
        try:
            with open(ruta, "rb") as fh:
                for raw in fh:
                    for ev in parser.feed_line(raw.decode("utf-8", "replace").rstrip("\r\n")):
                        call = phonebar_extract_call(ev)
                        if not call:
                            continue
                        total += 1
                        ok, why = phonebar_accepts(cfg, call)
                        if ok:
                            aceptadas += 1
                        print("%-8s %-14s %-11s %-22s %-14s %s"
                              % (call["kind"], call["number"], call["queue"][:11] or "-",
                                 call["uniqueid"] or "-", call["member"][:14] or "-",
                                 "SI" if ok else "no: " + why))
        except Exception as exc:
            print("  error en %s: %s" % (os.path.basename(ruta), exc))
    print("-" * 96)
    print("Eventos de llamada detectados: %d | abririan ventana: %d" % (total, aceptadas))
    print("Modo actual: phonebar.evento_apertura = %r"
          % (cfg["phonebar"].get("evento_apertura")))
    return 0


def cmd_espiar_log(cfg):
    """Sigue el log en vivo y muestra lo que detecta, sin abrir ventanas."""
    print("Vigilando %s" % ((cfg["phonebar"].get("log_file") or "").strip()
                            or PHONEBAR_DEFAULT_LOG))
    print("Haz una llamada de prueba. Ctrl+C para salir.\n")
    w = PhoneBarLogWatcher(cfg, None, from_start=False)
    w.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nFin.")
    return 0


def cmd_list_glpi(cfg):
    """Muestra entidades, categorias ITIL y el usuario de la sesion."""
    try:
        c = GlpiClient(cfg)
        c.init_session()
    except Exception as exc:
        print("ERROR:", exc)
        return 1
    try:
        try:
            sess = (c.get("getFullSession") or {}).get("session") or {}
            print("Usuario de la sesion: %s (users_id_requester = %s)"
                  % (sess.get("glpiname"), sess.get("glpiID")))
        except Exception as exc:
            print("No se pudo leer getFullSession:", exc)

        print("\nEntidades (glpi.entities_id):")
        try:
            for e in c.get("Entity", range="0-200") or []:
                print("  %-6s %s" % (e.get("id"), e.get("completename") or e.get("name")))
        except Exception as exc:
            print("  error:", exc)

        print("\nCategorias ITIL (para ui.categorias):")
        try:
            cats = c.get("ITILCategory", range="0-500") or []
            if not cats:
                print("  (ninguna definida)")
            for cat in cats:
                print("  %-6s %s" % (cat.get("id"), cat.get("completename") or cat.get("name")))
            print("\n  Bloque listo para copiar en config.json -> ui.categorias:")
            print("  " + json.dumps(
                {(cat.get("completename") or cat.get("name")): cat.get("id") for cat in cats},
                indent=2, ensure_ascii=False).replace("\n", "\n  "))
        except Exception as exc:
            print("  error:", exc)
        return 0
    finally:
        c.kill_session()


def _ancestros(entidades, eid):
    """Ids de las entidades padre de eid, de la mas cercana a la raiz."""
    out, actual, guarda = [], entidades.get(eid, {}).get("padre"), 0
    while actual is not None and guarda < 50:
        out.append(actual)
        actual = entidades.get(actual, {}).get("padre")
        guarda += 1
    return out


def _visibles_en(items, entidades, destino):
    """Aplica la regla de GLPI: un item es visible en su propia entidad, y en las
    hijas solo si esta marcado como recursivo."""
    permitidas = set(_ancestros(entidades, destino))
    out = {}
    for nombre, (iid, ent, rec) in items.items():
        if ent == destino or (ent in permitidas and rec):
            out[nombre] = iid
    return out


def cmd_autoconfig(cfg):
    """Rellena entidades, categorias, ubicaciones y solicitante desde GLPI,
    calculando que es visible en cada entidad."""
    c = GlpiClient(cfg)
    c.init_session()
    try:
        sess = (c.get("getFullSession") or {}).get("session") or {}
        uid = int(sess.get("glpiID") or 0)

        entidades = {}
        for r in (c.get("Entity", range="0-500") or []):
            padre = r.get("entities_id")
            entidades[int(r["id"])] = {
                "nombre": r.get("completename") or r.get("name"),
                "padre": None if padre in (None, "") else int(padre),
            }

        def cargar(tipo, rango):
            out = {}
            for r in (c.get(tipo, range=rango) or []):
                if not r.get("id"):
                    continue
                nombre = r.get("completename") or r.get("name")
                out[nombre] = (int(r["id"]), int(r.get("entities_id") or 0),
                               bool(int(r.get("is_recursive") or 0)))
            return out

        cats = cargar("ITILCategory", "0-500")
        locs = cargar("Location", "0-2000")

        # Correos -> usuario, para el campo Solicitante del formulario.
        usuarios = {}
        for u in (c.get("User", range="0-5000") or []):
            if u.get("id"):
                nombre = " ".join(x for x in (u.get("firstname"), u.get("realname")) if x)
                usuarios[int(u["id"])] = nombre or u.get("name") or ""
        correos = {}
        for e in (c.get("UserEmail", range="0-5000") or []):
            direccion = str(e.get("email") or "").strip().lower()
            duenio = int(e.get("users_id") or 0)      # no reutilizar 'uid': es el de la sesion
            if direccion and duenio:
                correos[direccion] = {"id": duenio, "nombre": usuarios.get(duenio, "")}
    finally:
        c.kill_session()

    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    with open(CONFIG_PATH + ".bak", "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)

    ui = data.setdefault("ui", {})
    ui["categorias"] = {n: v[0] for n, v in cats.items()}
    ui["sucursales_locations"] = {n: v[0] for n, v in locs.items()}
    ui["sucursales"] = sorted(locs.keys())
    ui["correos_usuarios"] = correos
    # Dominios ordenados por cuantos usuarios tiene cada uno.
    cuenta = {}
    for direccion in correos:
        if "@" in direccion:
            d = "@" + direccion.split("@", 1)[1]
            cuenta[d] = cuenta.get(d, 0) + 1
    if cuenta:
        ui["dominios_correo"] = [d for d, _ in sorted(cuenta.items(),
                                                      key=lambda kv: (-kv[1], kv[0]))]

    # Si no hay entidades configuradas, se ofrecen todas las de GLPI.
    if not ui.get("entidades"):
        ui["entidades"] = {d["nombre"].split(" > ")[-1]: eid
                           for eid, d in sorted(entidades.items())}

    ui["por_entidad"] = {}
    for eid in sorted(entidades):
        ui["por_entidad"][str(eid)] = {
            "categorias": _visibles_en(cats, entidades, eid),
            "sucursales": _visibles_en(locs, entidades, eid),
        }
    if uid:
        data.setdefault("glpi", {})["users_id_requester"] = uid

    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)

    print("config.json actualizado (respaldo en config.json.bak)")
    print("  categorias ITIL        : %d" % len(cats))
    print("  ubicaciones            : %d" % len(locs))
    print("  correos de usuarios    : %d" % len(correos))
    print("  dominios detectados    : %s" % ", ".join(ui.get("dominios_correo") or []))
    print("  users_id_requester     : %s (%s)\n" % (uid, sess.get("glpiname")))
    print("Visibilidad real por entidad (regla de recursividad de GLPI):")
    print("  %-4s %-38s %-11s %s" % ("ID", "ENTIDAD", "CATEGORIAS", "UBICACIONES"))
    for eid in sorted(entidades):
        b = ui["por_entidad"][str(eid)]
        marca = "  <- en uso" if eid in [int(v) for v in ui["entidades"].values()] else ""
        print("  %-4s %-38s %-11s %s%s"
              % (eid, entidades[eid]["nombre"][:38], len(b["categorias"]),
                 len(b["sucursales"]), marca))
    return 0


def cmd_retry_pending(cfg):
    d = os.path.join(APP_DIR, cfg["log"].get("pending_dir", "pendientes"))
    if not os.path.isdir(d):
        print("No hay pendientes.")
        return 0
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    if not files:
        print("No hay pendientes.")
        return 0
    rc = 0
    for f in files:
        p = os.path.join(d, f)
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            att = [a for a in data.get("attachments", []) if os.path.isfile(a)]
            tid, errs = submit_to_glpi(cfg, data["fields"], att, progress=print)
            print("%s -> ticket #%s%s" % (f, tid, (" adjuntos con error: %s" % errs) if errs else ""))
            os.rename(p, p + ".enviado")
        except Exception as exc:
            print("%s -> ERROR: %s" % (f, exc))
            rc = 1
    return rc


def cmd_test(cfg, number):
    app = App(cfg)
    app.dispatcher.offer(number, call_id="test-local", source="test")
    app.run()
    return 0


def main():
    global CONFIG
    ap = argparse.ArgumentParser(description="Popup xCally -> ticket GLPI")
    ap.add_argument("--test", metavar="NUMERO", help="abre la ventana con un numero de prueba")
    ap.add_argument("--ping", metavar="NUMERO", help="simula una llamada contra el listener")
    ap.add_argument("--check-glpi", action="store_true", help="valida las credenciales GLPI")
    ap.add_argument("--listar-glpi", action="store_true", dest="listar_glpi",
                    help="lista entidades y categorias ITIL de GLPI")
    ap.add_argument("--autoconfig", action="store_true",
                    help="rellena categorias, ubicaciones y solicitante desde GLPI")
    ap.add_argument("--retry-pending", action="store_true", help="reenvia los borradores fallidos")
    ap.add_argument("--probar-log", nargs="?", const="*", metavar="ARCHIVO",
                    dest="probar_log",
                    help="analiza los log historicos de la PhoneBar y lista las llamadas")
    ap.add_argument("--espiar-log", action="store_true", dest="espiar_log",
                    help="sigue el log de la PhoneBar en vivo sin abrir ventanas")
    args = ap.parse_args()

    CONFIG = load_config()

    if args.ping:
        return cmd_ping(CONFIG, args.ping)
    if args.check_glpi:
        return cmd_check_glpi(CONFIG)
    if args.listar_glpi:
        return cmd_list_glpi(CONFIG)
    if args.autoconfig:
        return cmd_autoconfig(CONFIG)
    if args.probar_log:
        return cmd_probar_log(CONFIG, args.probar_log)
    if args.espiar_log:
        return cmd_espiar_log(CONFIG)
    if args.retry_pending:
        return cmd_retry_pending(CONFIG)
    if args.test:
        return cmd_test(CONFIG, args.test)

    app = App(CONFIG)

    if CONFIG["listener"].get("enabled", True):
        try:
            start_listener(CONFIG, app.dispatcher)
        except OSError as exc:
            msg = ("No se pudo abrir el puerto %s: %s\n\nProbablemente el programa ya esta "
                   "corriendo. Cierra la otra instancia, o pon listener.enabled en false "
                   "si solo usaras la deteccion por el log de la PhoneBar."
                   % (CONFIG["listener"].get("port"), exc))
            log(msg)
            try:
                messagebox.showerror(APP_NAME, msg)
            except Exception:
                pass
            return 1

    if CONFIG["phonebar"].get("enabled", True):
        PhoneBarLogWatcher(CONFIG, app.dispatcher).start()
    if CONFIG["xcally"].get("enabled"):
        XCallyPoller(CONFIG, app.dispatcher).start()
    log("Programa residente iniciado. Esperando llamadas... (Ctrl+C para salir)")
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    log("Programa finalizado.")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
