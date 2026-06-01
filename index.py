from fastapi import FastAPI, Header, HTTPException, Request
from supabase import create_client
import stripe, os, httpx, secrets
from dotenv import load_dotenv
from datetime import datetime
from pydantic import BaseModel, Field
import asyncio
import re
from botasaurus.request import request, Request
from botasaurus.browser import browser, Driver
import time

botasaurus_semaphore = asyncio.Semaphore(1)

@request(
    cache=False,
    output=None,
    create_error_logs=False,
    proxy=os.getenv("PROXY")
)
def letterboxd_request(req: Request, data):
    response = req.get(
        data,
        headers={
            "Referer": "https://www.google.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
    )
    print(response.status_code)
    return response.json()

class SeleccionarRequest(BaseModel):
    movie_title: str

load_dotenv()

# stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
app = FastAPI()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

# try:
#     test = supabase.table('result_cache').select('*').limit(1).execute()
#     print('Connection succesful! Sample data: ', test.data)
# except Exception as e:
#     print('Connection failed: ', e)

@app.get('/')
def index():
    return {'message': 'Hola, Ivys!'}


# ── Generar token ──────────────────────────────────────
def generar_token():
    return secrets.token_hex(32)


# ── Validar acceso ─────────────────────────────────────
async def validar_acceso(token: str):
    print("Accediendo a función validar_acceso")
    result = supabase.table("tokens")\
        .select("*, users(id)")\
        .eq("token", token)\
        .eq("is_active", True)\
        .single()\
        .execute()

    if not result.data:
        raise HTTPException(status_code=403, detail="Token inválido o inactivo")
    

    return result.data

# ── Check token status ─────────────────────────────────────
@app.get("/check")
async def check(x_api_token: str = Header(...)):
    token_data = await validar_acceso(x_api_token)

    print("Llave validada correctamente!")

    return token_data['users']['id']

# ── Buscar películas ─────────────────────────────────────
@app.get("/buscar")
async def buscar(
            movie_title: str ,
            x_api_token: str = Header(...)):
    
    token_data = await validar_acceso(x_api_token)

    movie_dict = {}

    async with httpx.AsyncClient() as client:
        title_input = movie_title.strip().replace(' ', '+')
    
        respuesta = await client.get(
            f"https://letterboxd9.p.rapidapi.com/api/letterboxd/search?input={title_input}&searchMethod=Autocomplete&include=FilmSearchItem&excludeMemberFilmRelationships=true",
            headers={
                "x-rapidapi-key": os.getenv('RAPIDAPI_KEY'),
                "x-rapidapi-host": "letterboxd9.p.rapidapi.com"
            }
        )
    
    res = respuesta.json()
    
    items = res.get('items', [])
    for i in items:
        film = i.get('film', {})
        link = film.get('link', '')
        try:
            poster_link = film.get('poster', {}).get('sizes', [])[0].get('url', )
        except IndexError:
            poster_link = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wCEAAkGBxAQEBUQEBAVFRUVFRUVFRUVFRUVFRUVFRUWFhUVFRUYHSggGBolGxUVITEhJSkrLi4uFx8zODMsNygtLisBCgoKDg0OGhAQGi0fHyUtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLf/AABEIAAEAAQMBEQACEQEDEQH/xAAXAAADAQAAAAAAAAAAAAAAAAAAAQID/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEAMQAAAB6AAAAP/EABQQAQAAAAAAAAAAAAAAAAAAACD/2gAIAQEAAT8Af//EABQRAQAAAAAAAAAAAAAAAAAAACD/2gAIAQIBAT8Af//EABQRAQAAAAAAAAAAAAAAAAAAACD/2gAIAQMBAT8Af//Z"
        id = film.get('id', '')
        name = film.get('fullDisplayName', '')
        movie_dict[id] = {'name': name, 'link': link, 'poster': poster_link}

    supabase.table('result_cache').upsert({
        "user_id": token_data["users"]["id"],
        "results": movie_dict,
        "updated_at": datetime.now().isoformat()
    }, on_conflict="user_id").execute()

    supabase.table("tokens")\
        .update({"requests_used": token_data["requests_used"] + 1})\
        .eq("token", x_api_token)\
        .execute()
    
    movie_list = {x['name']: x.get('poster', '') for x in movie_dict.values()}

    return movie_list


# ── Seleccionar ─────────────────────────────────────
@app.post("/seleccionar")
async def seleccionar(body: SeleccionarRequest, x_api_token: str = Header(...)):
    token_data = await validar_acceso(x_api_token)

    response = supabase.table("result_cache")\
        .select("results")\
        .eq("user_id", token_data["users"]["id"])\
        .single()\
        .execute()
    
    movie_dict = response.data['results']

    selected_movie = next(
        ({"id": id, **data} for id, data in movie_dict.items() if data["name"] == body.movie_title),
        None
    )

    async with httpx.AsyncClient() as client:
        response_html = await client.get(selected_movie['link'])
        html = response_html.text
    
    uid_match = re.search(r'"uid":"film:\d\d\d+"', html)

    time.sleep(1.3)

    if uid_match:
        uid = re.search(r'\d+', uid_match.group(0)).group(0)

        letterboxd_link = f"https://letterboxd.com/s/production-availabilities?productionUID=film:{uid}&locale=MEX"

        async with botasaurus_semaphore:
            loop = asyncio.get_event_loop()
            response_stream = await loop.run_in_executor(None, letterboxd_request, letterboxd_link)

        data = response_stream
        
        streaming_list = data['best']['stream']
        streaming_options = [x['name'] for x in streaming_list]

        if streaming_options:
            return streaming_options
        else:
            return "No se encontraron opciones de streaming para este título"

    else:
        uid = None
        raise HTTPException(status_code=404, detail="No se encontró la película seleccionada")



# ── Crear usuario ──────────────────────────────────────
@app.post("/crear-usuario")
async def crear_usuario(body: dict):
    usuario = supabase.table("users")\
        .insert({"email": body["email"]})\
        .execute().data[0]

    nuevo_token = generar_token()
    supabase.table("tokens")\
        .insert({"user_id": usuario["id"], "token": nuevo_token})\
        .execute()

    return {"token": nuevo_token}