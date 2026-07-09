"""CoWork API application entrypoint."""
from fastapi import FastAPI

from .database import Base, engine
from .errors import AppError, app_error_handler
from .routers import admin, auth, bookings, health, rooms

Base.metadata.create_all(bind=engine)

from .database import SessionLocal
from .services.stats import init_stats
db = SessionLocal()
try:
    init_stats(db)
finally:
    db.close()

app = FastAPI(title="CoWork API", version="1.0.0")

app.add_exception_handler(AppError, app_error_handler)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(rooms.router)
app.include_router(bookings.router)
app.include_router(admin.router)
