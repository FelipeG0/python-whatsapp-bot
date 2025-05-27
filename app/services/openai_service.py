import json
from openai import OpenAI
import shelve
from dotenv import load_dotenv
import os
import time
import logging
import requests

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID")
client = OpenAI(api_key=OPENAI_API_KEY)


def upload_file(path):
    # Upload a file with an "assistants" purpose
    file = client.files.create(
        file=open("../../data/faq.pdf", "rb"), purpose="assistants"
    )


def create_assistant(file):
    """
    You currently cannot set the temperature for Assistant via the API.
    """
    assistant = client.beta.assistants.create(
        name="WhatsApp Murrah Assistant",
        instructions="Eres un asistente útil de WhatsApp que puede ayudar a los clientes que quieren hacer un pedido en nuestro restaurante Murrah. Usa tu base de conocimientos para responder de la mejor manera posible a las preguntas de los clientes. Si no sabes la respuesta, simplemente di que no puedes ayudar con esa pregunta y aconseja contactar al anfitrión directamente. Sé amigable y divertido.",
        tools=[{"type": "retrieval"}],
        model="gpt-4o-mini",
        file_ids=[file.id],
    )
    return assistant


# Use context manager to ensure the shelf file is closed properly
def check_if_thread_exists(wa_id):
    with shelve.open("threads_db") as threads_shelf:
        return threads_shelf.get(wa_id, None)


def store_thread(wa_id, thread_id):
    with shelve.open("threads_db", writeback=True) as threads_shelf:
        threads_shelf[wa_id] = thread_id


def run_assistant(thread, name):
    assistant = client.beta.assistants.retrieve(OPENAI_ASSISTANT_ID)

    if verificar_menu_enviado(thread.id):
        menu_enviado = True
    else:
        menu_enviado = False

    estructura_json = """
    {
        "cliente": "nombre_del_cliente",
        "pedido": [
            {"producto": "nombre_del_producto", "cantidad": cantidad},
            {"producto": "nombre_del_producto", "cantidad": cantidad}
        ],
        "hora_pedido": "2025-10-01T12:00:00Z (aqui va la hora del pedido)",
        "direccion": "direccion_del_cliente"
    }
    """

    run = client.beta.threads.runs.create(
        thread_id=thread.id,
        assistant_id=assistant.id,
        instructions=f"Estás teniendo una conversación con {name}. Primero espera a que se muestre el menú, y luego espera las instrucciones del cliente sobre su pedido. \
Cuando ya tengas todas las instrucciones, por último pide la dirección de la vivienda para poder llevar el pedido. \
Cuando el cliente diga explícitamente 'CONFIRMAR', recuérdale que debe decir 'CONFIRMAR' para finalizar. \
Luego manda el siguiente JSON y solo el JSON:\n {estructura_json}\n\
Ahora espera la respuesta del cliente.",
    )

    while run.status != "completed":
        time.sleep(0.5)
        run = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)

    messages = client.beta.threads.messages.list(thread_id=thread.id)
    new_message = messages.data[0].content[0].text.value
    logging.info(f"Mensaje generado: {new_message}")
    response = new_message

    if not menu_enviado:
        menu = cargar_menu_desde_txt()
        response = f"¡Hola {name}! ¡Bienvenido a Murrah! \nAquí está el menú: \n{menu}"

        client.beta.threads.messages.create(
            thread_id=thread.id,
            role="assistant",
            content=response,
        )
        marcar_menu_enviado(thread.id)

    elif "json" in new_message.lower():
        marcar_confirmacion(thread.id, True)
        import re

        try:
            match = re.search(r"```json\s*(\{.*?\})\s*```", new_message, re.DOTALL)
            if match:
                json_str = match.group(1)
                comanda_extraida = json.loads(json_str)

                # Validación de llaves requeridas
                required_keys = {"pedido", "direccion", "hora_pedido"}
                if not required_keys.issubset(comanda_extraida.keys()):
                    return "Faltan datos importantes para procesar tu pedido. Por favor asegúrate de incluir la dirección y la hora."

                estado_pedido[thread.id] = comanda_extraida
                logging.info(f"✅ Pedido guardado para {thread.id}: {estado_pedido[thread.id]}")
            else:
                raise ValueError("No se encontró bloque JSON válido en el mensaje")
        except Exception as e:
            logging.error(f"❌ Error al extraer comanda JSON: {e}")
            return "Hubo un error al procesar tu pedido. Por favor intenta nuevamente."

        comanda_json = finalizar_pedido(thread.id, name)
        logging.info(f"Comanda finalizada: {json.dumps(comanda_json, indent=4)}")
        estado_pedido[thread.id] = comanda_json
        response = f"Gracias por tu pedido, {name}. ¡Lo estamos procesando!"

    elif verificar_confirmacion(thread.id):
        response = "Tu pedido ya fue confirmado. Estamos procesando tu pedido."

    return response

def generate_response(message_body, wa_id, name):
    # Check if there is already a thread_id for the wa_id
    thread_id = check_if_thread_exists(wa_id)

    # If a thread doesn't exist, create one and store it
    if thread_id is None:
        logging.info(f"Creating new thread for {name} with wa_id {wa_id}")
        thread = client.beta.threads.create()
        store_thread(wa_id, thread.id)
        thread_id = thread.id

    # Otherwise, retrieve the existing thread
    else:
        logging.info(f"Retrieving existing thread for {name} with wa_id {wa_id}")
        thread = client.beta.threads.retrieve(thread_id)

    # Add message to thread
    message = client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=message_body,
    )

    # Run the assistant and get the new message
    new_message = run_assistant(thread, name)

    return new_message

# Diccionario que almacena el pedido del cliente
estado_pedido = {}

def finalizar_pedido(wa_id, name):
    """
    Función para finalizar el pedido del cliente y generar el JSON completo.
    """
    if wa_id not in estado_pedido:
        logging.warning(f"No hay pedido para {wa_id}")
        return None

    datos = estado_pedido[wa_id]

    comanda_json = {
        "cliente": name,
        "pedido": datos["pedido"],
        "direccion": datos["direccion"],
        "hora_pedido": datos["hora_pedido"]
    }

    try:
        response = requests.post("http://localhost:5001/comandas", json=comanda_json, timeout=5)
        response.raise_for_status()
        logging.info("✅ Comanda enviada correctamente.")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Error al enviar comanda: {e}")
        return None

    logging.info(f"📦 Comanda generada:\n{json.dumps(comanda_json, indent=4)}")
    return comanda_json

def agregar_producto_pedido(wa_id, producto, cantidad, precio):
    """
    Función para agregar un producto al pedido del cliente.
    Si el producto ya está en el pedido, actualiza la cantidad.
    """
    if wa_id not in estado_pedido:
        estado_pedido[wa_id] = []

    # Buscar si el producto ya está en el pedido
    producto_existente = next((item for item in estado_pedido[wa_id] if item['producto'] == producto), None)
    
    if producto_existente:
        # Si el producto ya existe, actualizar la cantidad
        producto_existente['cantidad'] += cantidad
    else:
        # Si el producto no existe, agregarlo
        estado_pedido[wa_id].append({
            'producto': producto,
            'cantidad': cantidad,
            'precio': precio
        })

    logging.info(f"Pedido actualizado para {wa_id}: {estado_pedido[wa_id]}")

def cargar_menu_desde_txt():
    """
    Lee el contenido de un archivo .txt y lo retorna como un string.

    Parámetros:
    ruta_archivo (str): Ruta del archivo de texto.

    Retorna:
    str: Contenido del archivo.
    """
    try:
        with open("app/utils/menu.txt", 'r', encoding='utf-8') as archivo:
            contenido = archivo.read()
        return contenido
    except FileNotFoundError:
        return "Error: El archivo no se encontró."
    except Exception as e:
        return f"Error al leer el archivo: {str(e)}"
    
def marcar_menu_enviado(thread_id):
    """
    Guarda en un archivo la información de que el menú ya ha sido enviado para este hilo.
    """
    with shelve.open('estado_conversacion.db', writeback=True) as db:
        db[thread_id] = True

def verificar_menu_enviado(thread_id):
    """
    Verifica si el menú ya fue enviado para este hilo de conversación.
    """
    with shelve.open('estado_conversacion.db') as db:
        return db.get(thread_id, False)

def marcar_confirmacion(wa_id, confirmado):
    """
    Guarda en un archivo la información de si el cliente ha confirmado o no su pedido.
    """
    with shelve.open('estado_confirmacion.db', writeback=True) as db:
        db[wa_id] = confirmado
        
def verificar_confirmacion(wa_id):
    """
    Verifica si el cliente ha confirmado su pedido.
    """
    with shelve.open('estado_confirmacion.db') as db:
        return db.get(wa_id, False)