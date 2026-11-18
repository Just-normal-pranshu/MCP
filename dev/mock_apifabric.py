"""Local stand-in for APIFabric's POST /access_token. Development only.

Accepts admin@demo.test / demo123 (Mode 1) and returns two companies; the first maps to the
fixture data in the Data API's dev instances.json.
Run: uvicorn mock_apifabric:app --port 5055
"""
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class SignIn(BaseModel):
    UserId: str
    Password: str | None = None
    Mode: int = 3


@app.post("/access_token")
def access_token(body: SignIn):
    if body.UserId != "admin@demo.test" or body.Password != "demo123":
        return {"Success": False, "Message": "Invalid user id or password", "Data": None}
    if body.Mode != 1:
        return {"Success": False, "Message": "No admin profile for this mode", "Data": None}
    return {"Success": True, "Message": "Record retirve successfully", "Data": {
        "Token": "demo-token", "TokenValidity": "2099-01-01T00:00:00",
        "Instances": [{"Id": "DEMO-INSTANCE-1", "Name": "zOrder Testing", "ConnString": "should-be-ignored"},
                      {"Id": "DEMO-INSTANCE-2", "Name": "Second Demo Co"}]}}


# ---- stand-in for the ERP "connection details" API (Token + Instance -> DB details). Dev only.
from fastapi import Header, HTTPException  # noqa: E402

CALLS = {"connection": 0}


@app.get("/internal/connection")
def connection(z_auth_token: str = Header(""), instance_id: str = Header("", alias="Instance_Id", convert_underscores=False),
               x_internal_key: str = Header("")):
    CALLS["connection"] += 1
    if x_internal_key != "dev-service-key":
        raise HTTPException(401, "bad service key")
    if z_auth_token != "demo-token" or instance_id != "DEMO-INSTANCE-1":
        return {"Success": False, "Message": "Invalid token or company", "Data": None}
    return {"Success": True, "Data": {"ServerName": "demo-sql", "Port": "1433", "DatabaseName": "DemoDb",
                                      "UserName": "erp_readonly", "Password": "p;ss{w}ord",
                                      "Name": "zOrder Testing", "TokenValidity": "2099-01-01T00:00:00+05:30"}}


@app.get("/internal/calls")
def calls():
    return CALLS
