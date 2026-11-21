import io
import hashlib

import imagehash
from PIL import Image, ImageOps, UnidentifiedImageError

# Pontos de partida (a calibrar no teste): distância 0 = idêntica, máximo = 64.
LIMITE_PROVAVEL = 10
LIMITE_POSSIVEL = 18


def calcular_phash(conteudo: bytes) -> str:
    try:
        imagem = Image.open(io.BytesIO(conteudo))
        imagem = ImageOps.exif_transpose(imagem)  # corrige fotos "deitadas"
    except (UnidentifiedImageError, OSError) as erro:
        raise ValueError("O arquivo não é uma imagem válida.") from erro
    return str(imagehash.phash(imagem))


def calcular_sha256(conteudo: bytes) -> str:
    return hashlib.sha256(conteudo).hexdigest()


def distancia(hash_a: str, hash_b: str) -> int:
    return int(imagehash.hex_to_hash(hash_a) - imagehash.hex_to_hash(hash_b))


def classificar(dist: int) -> str:
    if dist <= LIMITE_PROVAVEL:
        return "provável cópia"
    if dist <= LIMITE_POSSIVEL:
        return "possível semelhança"
    return "diferente"


def melhores_correspondencias(phash_suspeito: str, hashes_catalogo, limite: int = 5):
    melhores = {}
    for linha in hashes_catalogo:
        dist = distancia(phash_suspeito, linha["phash"])
        atual = melhores.get(linha["conteudo_id"])
        if atual is None or dist < atual["distancia"]:
            melhores[linha["conteudo_id"]] = {
                "conteudo_id": linha["conteudo_id"],
                "criadora": linha["criadora"],
                "arquivo": linha["arquivo"],
                "distancia": dist,
                "classificacao": classificar(dist),
            }
    return sorted(melhores.values(), key=lambda r: r["distancia"])[:limite]