
import os
import certifi
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

uri = os.getenv("MONGO_URI")

if not uri:
    raise ValueError("MONGO_URI is missing from your .env file")

try:
    client = MongoClient(
        uri,
        tls=True,
        tlsCAFile=certifi.where(),
        serverSelectionTimeoutMS=15000,
    )

    client.admin.command("ping")
    print("SUCCESS: MongoDB Atlas connection established!")

except Exception as error:
    print(f"Connection failed: {error}")

finally:
    if "client" in locals():
        client.close()
