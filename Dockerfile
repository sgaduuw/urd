# 3.11 is a floor, not a preference: _ts relies on its fromisoformat parsing both
# the "+0000" shape Jira sends on issue fields and the "Z" shape it sends on the
# sprint field. On 3.10 it raises.
FROM python:3.14-slim

# Two dependencies, which is the whole list. duckdb for the database, flask for
# the server. Anything else is a decision, not an install.
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
RUN groupadd --gid 10001 urd && useradd --uid 10001 --gid urd --no-create-home urd \
    && mkdir -p /var/lib/urd && chown urd:urd /var/lib/urd
COPY urd.py charts.py render.py projects.py wizard.py webapp.py ./
COPY views_report.py views_jobs.py views_wizard.py ./
COPY capacity.py capacity_history.py views_capacity.py ./
COPY vendor/ ./vendor/

# The volume, not a layer: the database is the only state and a restart must keep
# it. Nothing about the scope or the token is baked in; both arrive at runtime.
ENV URD_VOLUME=/var/lib/urd
VOLUME /var/lib/urd
USER 10001:10001

EXPOSE 8731
CMD ["python", "urd.py", "serve", "--host", "0.0.0.0", "--port", "8731"]
