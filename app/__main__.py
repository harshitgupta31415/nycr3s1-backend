import os

import uvicorn


def main() -> None:
    """Run the API server using Cloud Run's assigned port."""
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        # Preserve application/error logs without writing one line per request.
        access_log=False,
    )


if __name__ == "__main__":
    main()
