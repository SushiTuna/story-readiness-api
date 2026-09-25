from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.routers import assess as assess_router
from app.routers import sources as sources_router

app = FastAPI(title="Story Readiness API", version="0.1.0")

app.include_router(assess_router.router, prefix="/api")
app.include_router(sources_router.router, prefix="/api")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
