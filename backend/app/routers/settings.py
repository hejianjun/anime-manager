from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AppSetting
from ..schemas import SettingsPatch
from ..source_settings import get_all_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def get_settings(db: Session = Depends(get_db)):
    return get_all_settings(db)


@router.patch("")
def patch_settings(
    payload: SettingsPatch,
    db: Session = Depends(get_db),
):
    for key, value in payload.model_dump(exclude_unset=True).items():
        row = db.get(AppSetting, key)
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=key, value=value))
    db.commit()
    return get_all_settings(db)
