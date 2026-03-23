"""
GramGPT API — routers/auth.py
Эндпоинты: регистрация, вход, refresh, выход
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from schemas.user import UserRegister, UserLogin, TokenPair, TokenRefresh, UserOut, PasswordChange
from services import auth as auth_svc
from routers.deps import get_current_user
from models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=201)
async def register(data: UserRegister, db: AsyncSession = Depends(get_db)):
    """Регистрация нового пользователя"""
    user = await auth_svc.create_user(db, data.email, data.password)
    return user


@router.post("/login", response_model=TokenPair)
async def login(data: UserLogin, db: AsyncSession = Depends(get_db)):
    """Вход — возвращает access + refresh токены"""
    user = await auth_svc.authenticate_user(db, data.email, data.password)

    access_token  = auth_svc.create_access_token(user.id)
    refresh_token = auth_svc.create_refresh_token()
    await auth_svc.save_refresh_token(db, user.id, refresh_token)

    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(data: TokenRefresh, db: AsyncSession = Depends(get_db)):
    """Обновление токенов по refresh token"""
    user = await auth_svc.verify_refresh_token(db, data.refresh_token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh токен недействителен или истёк"
        )

    access_token      = auth_svc.create_access_token(user.id)
    new_refresh_token = auth_svc.create_refresh_token()
    await auth_svc.save_refresh_token(db, user.id, new_refresh_token)

    return TokenPair(
        access_token=access_token,
        refresh_token=new_refresh_token
    )


@router.post("/logout")
async def logout(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Выход — отзывает все refresh токены"""
    await auth_svc.revoke_all_tokens(db, current_user.id)
    return {"detail": "Выход выполнен успешно"}


@router.get("/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    """Информация о текущем пользователе"""
    return current_user


@router.post("/change-password")
async def change_password(
    data: PasswordChange,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Смена пароля"""
    if not auth_svc.verify_password(data.old_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Неверный текущий пароль")

    current_user.password_hash = auth_svc.hash_password(data.new_password)
    await auth_svc.revoke_all_tokens(db, current_user.id)
    return {"detail": "Пароль изменён. Войдите заново."}
