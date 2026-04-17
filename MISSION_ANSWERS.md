# Day 12 Lab - Mission Answers

## Part 1: Localhost vs Production

### Exercise 1.1: Anti-patterns found in `01-localhost-vs-production/develop/app.py`
1. **Hardcoded Secrets**: API keys and database URLs are written directly in the code, which is a major security risk if pushed to a repository.
2. **Missing Configuration Management**: Variables like `DEBUG` and `MAX_TOKENS` are hardcoded instead of being loaded from environment variables or a config file (violates 12-Factor App principles).
3. **Inefficient Logging**: Uses `print()` statements instead of a structured logging library. This makes it hard to filter logs by level or parse them in production (e.g., using JSON format).
4. **Missing Health Checks**: There is no `/health` or `/ready` endpoint, so container orchestrators (like Docker or Kubernetes) wouldn't know if the app is healthy or if it should be restarted.
5. **Fixed Netwoking Config**: The `host` is set to `localhost` (making it inaccessible from outside the container) and the `port` is hardcoded to `8000` instead of reading from the `PORT` environment variable provided by cloud platforms.
6. **Debug Mode in Production**: `reload=True` is enabled by default, which is a performance and security risk in a production environment.

### Exercise 1.3: Comparison table

| Feature | Develop | Production | Why Important? |
|---------|---------|------------|----------------|
| **Config** | Hardcoded in code | Environment variables (.env) | Flexibility to change settings without changing code; security for secrets. |
| **Health Check** | None | `/health` & `/ready` endpoints | Allows the platform to monitor app status and restart if it crashes. |
| **Logging** | `print()` statements | Structured JSON logging | Easier to search, filter, and analyze logs in production tools (ELK, Loki). |
| **Shutdown** | Abrupt (Ctrl+C) | Graceful (SIGTERM handling) | Ensures in-flight requests are completed and resources are cleaned up before closing. |
| **Networking** | `localhost:8000` | `0.0.0.0:${PORT}` | Required for the app to be reachable in a container or on cloud platforms like Railway/Render. |

## Part 2: Docker

### Exercise 2.1: Dockerfile questions
1. **Base image**: `python:3.11` (Full distribution, based on Debian, roughly ~1GB).
2. **Working directory**: `/app` (This is where the application code will live inside the container).
3. **Why COPY requirements.txt first?**: To optimize build times. By copying `requirements.txt` before the rest of the code, Docker can cache the installed dependencies. If the code changes but `requirements.txt` stays the same, Docker skips the expensive `pip install` step.
   - `ENTRYPOINT`: Sets the main executable for the container; difficult to override. It's usually used for the main executable of the container. Arguments passed to `docker run` are appended to the `ENTRYPOINT` command.

### Exercise 2.3: Image size comparison
- **Develop Image Size**: 1.66 GB
- **Production Image Size**: 214 MB
- **Difference**: 87% reduction in size.
- **Why?**: The production version uses `python:3.11-slim` (multi-stage) which stripped away 1.4GB of build-time dependencies.

## Part 3: Cloud Deployment

### Exercise 3.1: Railway Deployment
- **Platform Choice**: **Railway** was chosen for its simplicity and built-in Redis support.
- **Process**:
  1. Connected GitHub repository to Railway.
  2. Added a **Redis** service to the project canvas.
  3. Configured environment variables: `PORT=8000`, `AGENT_API_KEY`.
  4. Railway automatically detected the Dockerfile and deployed the service.
- **Verification**: The agent is accessible via:
  - URL: https://2a202600188-nguyenngochung-day12-production.up.railway.app
  - Screenshot: [Railway Dashboard](screenshots/dashboard.png)
Health and Ready checks return 200 OK.

### Exercise 3.2: Comparison between Railway and Render
- **Railway**: Better for quick prototypes and "Canvas" based orchestration where you can see all services (Agent, Redis, DB) visually.
- **Render**: Stronger support for **Infrastructure as Code** using `render.yaml`, making it easier to replicate environments and manage complex configurations in Git.

## Part 4: API Security

### Exercise 4.1: API Key authentication
- **Where is the API key checked?**: In the `verify_api_key` function (located in `app.py`), which is injected into protected endpoints using FastAPI's `Depends`.
- **What happens if the key is wrong?**: If the key is missing from the header, it returns a `401 Unauthorized` error. If the key is present but incorrect, it returns a `403 Forbidden` error.
- **How to rotate the key?**: The key is loaded from the `AGENT_API_KEY` environment variable. To rotate it, simply update the environment variable value on the server or cloud platform and restart the service.

### Exercise 4.3: Rate limiting
- **Algorithm used**: **Sliding Window Counter**. It uses a `deque` of timestamps for each user to track requests within a 60-second window.
- **Limits**:
  - Regular users: **10 requests per minute**.
  - Admin users: **100 requests per minute**.
- **Admin bypass**: Admins have a higher limit defined in a separate `RateLimiter` instance (`rate_limiter_admin`). The application checks the user's role (from the JWT) and applies the corresponding limiter.

### Exercise 4.4: Cost guard implementation
- **Approach**: The `CostGuard` class tracks usage based on token counts (estimated from the length of input and output text). It maintains both per-user and global budget records. Before each LLM call, it checks if the user or the global system has exceeded their daily budget (e.g., $1.00/day per user). Usage is recorded in-memory (in this demo version), but in production, this should be moved to Redis to ensure persistence and horizontal scalability.

## Part 5: Scaling & Reliability

### Exercise 5.1: Health checks
- **Liveness probe (`/health`)**: Used to tell the platform if the container is still running correctly. If this endpoint fails, the platform will restart the container.
- **Readiness probe (`/ready`)**: Used to tell the load balancer if the agent is ready to receive traffic. It returns 503 during startup or shutdown, preventing users from reaching an uninitialized service.

### Exercise 5.2: Graceful shutdown
- **Implementation**: The application uses the `lifespan` context manager in FastAPI and listens for the `SIGTERM` signal. When triggered, it stops accepting new requests (`_is_ready = False`) and waits for current "in-flight" requests to finish before finally exiting. This prevents users from receiving "Connection Refused" errors during a deployment or restart.

### Exercise 5.3: Stateless design
- **Why is in-memory state an anti-pattern?**: When scaling to multiple instances, each instance has its own local memory. If user session history or rate limit data is stored in memory, as the load balancer routes requests to different instances, the agent would "forget" the previous context or the user's rate limit status.
- **Solution**: Move all shared state (conversation history, rate limit counters, budget data) into a shared database or cache like **Redis**. This allows all instances to access the same data, making the individual agent processes interchangeable (stateless).

## Part 6: Final Project (Production AI Agent)

The final project combines all concepts learned throughout the lab into a cohesive, production-ready system.

### Key Features Implemented:
1.  **Stateless Session Management**: Conversation history is stored in Redis, allowing the agent to maintain context across multiple instances and restarts.
2.  **Scalable Rate Limiting**: Uses Redis Sorted Sets to implement a sliding window algorithm that works consistently across a load-balanced cluster.
3.  **Global Cost Guard**: Tracks token usage and spending in Redis Hashes, preventing budget overruns even with multiple agent replicas.
4.  **12-Factor Configuration**: Fully managed via environment variables using `Settings` (Pydantic style), making it easy to deploy across different environments.
5.  **Observability & Reliability**: 
    *   Structured JSON logging for automated parsing.
    *   Health (`/health`) and Readiness (`/ready`) probes for container orchestration.
    *   Graceful shutdown handling for `SIGTERM` signals.
6.  **Production Dockerization**: Uses a multi-stage Dockerfile to minimize image size and maximize security by excluding build-time tools from the final image.
