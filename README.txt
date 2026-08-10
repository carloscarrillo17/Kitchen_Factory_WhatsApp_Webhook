KITCHEN FACTORY - WEBHOOK WHATSAPP CRM

Token de verificación para Meta:
kf_crm_verify_2026

Cuando el backend esté publicado, en Meta debes poner:

URL de devolución de llamada:
https://TU-DOMINIO/webhook/whatsapp

Token de verificación:
kf_crm_verify_2026

Rutas:
GET  /                     prueba del servidor
GET  /webhook/whatsapp     verificación de Meta
POST /webhook/whatsapp     recepción de eventos
GET  /api/messages         últimos mensajes recibidos

Importante:
Meta necesita una URL HTTPS pública. localhost no sirve directamente.
Este proyecto ya está preparado para desplegarse en un hosting Python.
