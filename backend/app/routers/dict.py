"""悬停词卡 API。"""
from fastapi import APIRouter, HTTPException, Query

from ..services import dict as dict_svc

router = APIRouter(prefix="/api/dict", tags=["dict"])


@router.get("")
async def lookup(word: str = Query(...), ctx: str = Query(default="")):
    try:
        return await dict_svc.word_card(word, ctx)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"词卡生成失败：{str(e)[:200]}")
