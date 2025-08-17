# 🏥 MediCore AI Assistant Platform (Starter Kit)

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-blue?logo=docker)](https://www.docker.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-🚀-green)](https://fastapi.tiangolo.com/)

**MediCore** is an AI-powered hospital assistant starter platform with secure **OIDC SSO**, **JWT/JWKS auth enforcement**, and **FHIR integration**.  
This repo is a starter template to bootstrap enterprise-grade healthcare AI systems.

---

## ✨ Features

- 🔐 **Authentication & Security**
  - OIDC SSO support
  - Gateway-level JWT middleware
  - JWKS/RS256 validation
  - Role-based access control (`clinician`, `admin`)

- 📂 **FHIR Integration**
  - Patient, Encounter, Observation, MedicationRequest endpoints
  - Search endpoints with query param passthrough
  - SMART-on-FHIR ready design

- ⚡ **Caching Layer**
  - FHIR search responses cached in Postgres
  - Resource-specific TTLs (Patient/Encounter/Medication = 300s, Observations = 60s)

- 🧹 **Cache Invalidation**
  - Admin-only endpoints to flush cache globally or per patient
  - HIPAA-friendly (no stale data risk)

- 📦 **Dockerized**
  - Multi-service Docker Compose setup
  - Postgres included for persistence & caching

---

## 🚀 Quick Start

```bash
# clone your repo
git clone https://github.com/Rajput009/medicore-assistant.git
cd medicore-assistant

# create .env file (see .env.example)
cp backend/.env.example backend/.env

# build & start
docker compose -f deploy/docker/docker-compose.yml up --build
