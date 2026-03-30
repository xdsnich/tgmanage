# import httpx

# # ⚠️ ВСТАВЬ СЮДА СВОЙ КЛЮЧ ОТ GROQ (начинается на gsk_...)


# url = "https://api.groq.com/openai/v1/chat/completions"

# headers = {
#     "Authorization": f"Bearer {API_KEY}",
#     "Content-Type": "application/json"
# }

# data = {
#     # Используем мощную и бесплатную модель Llama 3
#     "model": "llama-3.3-70b-versatile", 
#     "messages": [
#         {"role": "user", "content": "Привет! Это тестовый запрос. Напиши слово ОК, если ты меня слышишь."}
#     ]
# }

# print("⏳ Отправляем тестовый запрос к Groq...")

# try:
#     with httpx.Client(timeout=10) as client:
#         response = client.post(url, headers=headers, json=data)
        
#     print(f"\nСтатус-код: {response.status_code}")
    
#     if response.status_code == 200:
#         print("🎉 УСПЕХ! Ключ Groq РАБОТАЕТ! Ответ нейросети:")
#         print(response.json()["choices"][0]["message"]["content"])
#     else:
#         print("❌ ОШИБКА. Сервер отклонил запрос. Причина:")
#         print(response.text)
        
# except Exception as e:
#     print(f"Критическая ошибка соединения: {e}")