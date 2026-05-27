Gmail MCP server setup

1) Create OAuth 2.0 Client ID in GCP (project: stockyapp-b7cdc)
   - Go to APIs & Services -> Credentials
   - Create OAuth 2.0 Client ID (Application type: Web application)
   - Add Authorized redirect URI: https://gmail.srv1694637.hstgr.cloud/oauth2callback
   - Download the JSON and save as `gcp-oauth.keys.json`

2) Upload the key to the VPS:

   scp gcp-oauth.keys.json root@srv1694637.hstgr.cloud:/root/.gmail-mcp/gcp-oauth.keys.json

3) Start the Gmail MCP container:

   ssh root@srv1694637.hstgr.cloud
   cd /root/docker/mcp-servers
   docker compose up -d

4) Run the auth flow (if the server doesn't open a browser automatically):

   # This prints a URL to visit in your browser; complete the consent flow.
   docker compose run --rm gmail-mcp npx -y @gongrzhe/server-gmail-autoauth-mcp auth

5) After successful auth the server will save credentials to `/root/.gmail-mcp/credentials.json` and Hermes will be able to connect.

Notes:
- Tokens/credentials are stored in `/root/.gmail-mcp/` on the VPS.
- I added an MCP entry to Hermes' /root/.hermes/config.yaml pointing to https://gmail.srv1694637.hstgr.cloud/mcp with `auth: oauth` and limited tools (drafts/reading/search).
- When you upload the `gcp-oauth.keys.json` file and run the auth command, paste the authorization URL into your browser (use scott@yopie.ca) to complete OAuth.
