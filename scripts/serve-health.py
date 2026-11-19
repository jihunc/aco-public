"""Local API uses the same explicit provider startup composition as the normal entrypoint."""

import uvicorn
from local_runtime import local_settings

from aco.core.application import configured_app
from aco.core.local_runtime import local_provider_selection

if __name__ == "__main__":
    app = configured_app(local_settings(), local_provider_selection())
    uvicorn.run(app, host="127.0.0.1", port=58000, access_log=False)
