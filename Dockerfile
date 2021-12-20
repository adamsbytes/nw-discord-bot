FROM python:slim
WORKDIR /opt/invasion-bot/
RUN adduser --disabled-password bot-user && chown -R bot-user /opt/invasion-bot
ADD .aws /home/bot-user/.aws
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY .env* .
COPY *.conf .
COPY *.py .
USER bot-user
CMD ["python", "./discord_bot.py"]