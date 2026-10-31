import os

# Must be set before any app module is imported (app.db builds the engine at import time).
os.environ.update({
    "DATABASE_URL": "sqlite:///./test.db",
    "API_KEY": "test-key",
    "CANDIDATE_NAME": "Test Candidate",
    "CANDIDATE_PHONE": "+91-0000000000",
    "CANDIDATE_LINKEDIN": "https://linkedin.com/in/test",
    "CANDIDATE_PORTFOLIO": "https://test.netlify.app",
    "RESUME_URL": "https://drive.google.com/file/d/abc123/view?usp=sharing",
    "GMAIL_ADDRESS": "me@gmail.com",
    "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop",
    "GEMINI_API_KEY": "",
    "GROQ_API_KEY": "",
    "APOLLO_API_KEY": "",
    "HUNTER_API_KEY": "",
    "COMPANY_BLOCKLIST": "Quantal",
})
