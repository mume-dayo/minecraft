
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import asyncio
import json
import os
from datetime import datetime, time
import re
from flask import Flask, jsonify
import threading

# Flask アプリケーション
app = Flask(__name__)

# ボット設定
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)

# スケジュールされたメッセージを保存する辞書
scheduled_messages = {}

# Flask ルート
@app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Discord Bot Status</title>
        <style>
            body { font-family: Arial, sans-serif; text-align: center; margin: 50px; }
            .status { font-size: 24px; margin: 20px 0; }
            .online { color: green; }
            .offline { color: red; }
        </style>
    </head>
    <body>
        <h1>Discord Bot Status</h1>
        <div id="status" class="status">確認中...</div>
        <script>
            fetch('/api/status')
                .then(response => response.json())
                .then(data => {
                    document.getElementById('status').innerHTML = data.message;
                    document.getElementById('status').className = 'status ' + (data.online ? 'online' : 'offline');
                });
        </script>
    </body>
    </html>
    '''

@app.route('/api/status')
def api_status():
    if bot.is_ready():
        return jsonify({
            'online': True,
            'message': 'Bot is online',
            'latency': round(bot.latency * 1000, 2),
            'guilds': len(bot.guilds)
        }), 200
    else:
        return jsonify({
            'online': False,
            'message': 'Bot is offline'
        }), 200

def run_flask():
    app.run(host='0.0.0.0', port=5000, debug=False)

@bot.event
async def on_ready():
    print(f'{bot.user} has logged in to Discord!')
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    check_scheduled_messages.start()

@bot.tree.command(name='http', description='HTTPエンドポイントを取得')
@app_commands.describe(address='取得するHTTPアドレス')
async def http_endpoint(interaction: discord.Interaction, address: str):
    """HTTPエンドポイントを取得"""
    await interaction.response.defer()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(address) as response:
                if response.status == 200:
                    content = await response.text()
                    # レスポンスが長すぎる場合は切り詰める
                    if len(content) > 1900:
                        content = content[:1900] + "..."
                    await interaction.followup.send(f"```\nHTTP Response from {address}:\nStatus: {response.status}\n\n{content}\n```")
                else:
                    await interaction.followup.send(f"HTTP Error: {response.status}")
    except Exception as e:
        await interaction.followup.send(f"Error connecting to {address}: {str(e)}")

@bot.tree.command(name='server', description='javaか統合かを見ます')
@app_commands.describe(server_address='サーバーアドレス')
async def server_command(interaction: discord.Interaction, server_address: str):
    """マインクラフトサーバー統合版/Javaを判定"""
    await interaction.response.defer()
    try:
        async with aiohttp.ClientSession() as session:
            # まずJava版の通常APIで確認
            java_url = f"https://api.mcsrvstat.us/2/{server_address}"
            async with session.get(java_url) as java_response:
                if java_response.status == 200:
                    java_data = await java_response.json()
                    if java_data.get('online', False):
                        await send_server_status(interaction, server_address, java_data, "Java版")
                        return
            
            # Java版がオフラインまたは存在しない場合、統合版のAPIで確認
            bedrock_url = f"https://api.mcsrvstat.us/bedrock/2/{server_address}"
            async with session.get(bedrock_url) as bedrock_response:
                if bedrock_response.status == 200:
                    bedrock_data = await bedrock_response.json()
                    if bedrock_data.get('online', False):
                        await send_server_status(interaction, server_address, bedrock_data, "統合版(Bedrock)")
                        return
            
            # どちらもオフラインの場合
            embed = discord.Embed(
                title=f"🔴 {server_address}",
                description="サーバーはオフラインだよ！確認しろ！",
                color=0xff0000
            )
            await interaction.followup.send(embed=embed)
                    
    except Exception as e:
        await interaction.followup.send(f"エラーが発生しました: {str(e)}")

async def send_server_status(interaction: discord.Interaction, server_address: str, data: dict, edition: str):
    """サーバー状態を送信する共通関数"""
    players = data.get('players', {})
    online_count = players.get('online', 0)
    max_count = players.get('max', 0)
    version = data.get('version', 'Unknown')
    
    embed = discord.Embed(
        title=f"🟢 {server_address}",
        description=f"たぶんサーバーはオンラインです ({edition})",
        color=0x00ff00
    )
    embed.add_field(name="プレイヤー数", value=f"{online_count}/{max_count}", inline=True)
    embed.add_field(name="マイクラのバージョン", value=version, inline=True)
    embed.add_field(name="エディション", value=edition, inline=True)
    
    if 'motd' in data:
        if isinstance(data['motd'], dict) and 'clean' in data['motd']:
            motd = data['motd']['clean'][0] if data['motd']['clean'] else "N/A"
        else:
            motd = str(data['motd'])
        embed.add_field(name="MOTD", value=motd, inline=False)
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name='mine', description='マインクラフトサーバーの状態を確認')
@app_commands.describe(
    server_address='マインクラフトサーバーのアドレス',
    bedrock='統合版(Bedrock)の場合はTrue',
    simple='シンプル形式で取得する場合はTrue（デフォルト）'
)
async def minecraft_server(interaction: discord.Interaction, server_address: str, bedrock: bool = False, simple: bool = True):
    """マインクラフトサーバーの状態を確認"""
    await interaction.response.defer()
    try:
        # APIエンドポイントを決定
        if simple:
            if bedrock:
                api_url = f"https://api.mcsrvstat.us/bedrock/2/{server_address}"
            else:
                api_url = f"https://api.mcsrvstat.us/2/{server_address}"
        else:
            api_url = f"https://api.mcsrvstat.us/3/{server_address}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    if simple:
                        # シンプル形式
                        if data.get('online', False):
                            players = data.get('players', {})
                            online_count = players.get('online', 0)
                            max_count = players.get('max', 0)
                            version = data.get('version', 'Unknown')
                            
                            embed = discord.Embed(
                                title=f"🟢 {server_address}",
                                description="サーバーはオンラインです",
                                color=0x00ff00
                            )
                            embed.add_field(name="プレイヤー", value=f"{online_count}/{max_count}", inline=True)
                            embed.add_field(name="バージョン", value=version, inline=True)
                            
                            if 'motd' in data:
                                motd = data['motd']['clean'][0] if isinstance(data['motd'], dict) else str(data['motd'])
                                embed.add_field(name="MOTD", value=motd, inline=False)
                                
                        else:
                            embed = discord.Embed(
                                title=f"🔴 {server_address}",
                                description="サーバーはオフラインです",
                                color=0xff0000
                            )
                        await interaction.followup.send(embed=embed)
                    else:
                        # 詳細形式
                        # JSON形式で全データを表示（長すぎる場合は切り詰め）
                        json_data = json.dumps(data, indent=2, ensure_ascii=False)
                        if len(json_data) > 1900:
                            json_data = json_data[:1900] + "..."
                        
                        await interaction.followup.send(f"```json\n{json_data}\n```")
                else:
                    await interaction.followup.send(f"APIエラー: {response.status}")
                    
    except Exception as e:
        await interaction.followup.send(f"エラーが発生しました: {str(e)}")

@bot.tree.command(name='ping', description='ボットのpingを取得')
async def ping_command(interaction: discord.Interaction):
    """ボットのpingを取得"""
    latency = round(bot.latency * 1000, 2)
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"レイテンシ: {latency}ms",
        color=0x00ff00
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='say', description='指定されたメッセージを送信')
@app_commands.describe(message='送信するメッセージ')
async def say_command(interaction: discord.Interaction, message: str):
    """指定されたメッセージを送信"""
    await interaction.response.send_message(message)

@bot.tree.command(name='setmessage', description='指定時刻にメッセージを送信するよう設定')
@app_commands.describe(
    hour='時（0-23）',
    minute='分（0-59）',
    second='秒（0-59）',
    everyone='@everyoneを付けるかどうか',
    message='送信するメッセージ'
)
async def set_message(interaction: discord.Interaction, hour: int, minute: int, second: int, everyone: bool, message: str):
    """指定時刻にメッセージを送信するよう設定"""
    try:
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            await interaction.response.send_message("正しい時刻を入力してください")
            return
        
        # スケジュールに追加
        schedule_time = time(hour, minute, second)
        schedule_id = f"{interaction.channel.id}_{len(scheduled_messages)}"
        
        scheduled_messages[schedule_id] = {
            'channel_id': interaction.channel.id,
            'time': schedule_time,
            'message': message,
            'everyone': everyone,
            'active': True
        }
        
        await interaction.response.send_message(f"✅ メッセージを {hour:02d}:{minute:02d}:{second:02d} に設定しました")
        
    except Exception as e:
        await interaction.response.send_message(f"エラーが発生しました: {str(e)}")

@bot.tree.command(name='stopmessage', description='全てのスケジュールされたメッセージを停止')
async def stop_message(interaction: discord.Interaction):
    """全てのスケジュールされたメッセージを停止"""
    count = 0
    for schedule_id in list(scheduled_messages.keys()):
        if scheduled_messages[schedule_id]['channel_id'] == interaction.channel.id:
            scheduled_messages[schedule_id]['active'] = False
            count += 1
    
    await interaction.response.send_message(f"✅ {count}個のスケジュールされたメッセージを停止しました")

@bot.tree.command(name='giveaway', description='ギブアウェイを開始')
@app_commands.describe(
    duration='ギブアウェイの時間（秒）',
    prize='賞品名'
)
async def giveaway(interaction: discord.Interaction, duration: int = 60, prize: str = "素敵な賞品"):
    """ギブアウェイを開始"""
    embed = discord.Embed(
        title="🎉 ギブアウェイ開始！",
        description=f"**賞品:** {prize}\n**時間:** {duration}秒\n\n🎁 をクリックして参加！",
        color=0xffd700
    )
    embed.set_footer(text=f"終了時刻: {duration}秒後")
    
    await interaction.response.send_message(embed=embed)
    message = await interaction.original_response()
    await message.add_reaction("🎁")
    
    # ギブアウェイ終了を待つ
    await asyncio.sleep(duration)
    
    # リアクションを取得
    message = await interaction.channel.fetch_message(message.id)
    reaction = discord.utils.get(message.reactions, emoji="🎁")
    
    if reaction and reaction.count > 1:  # ボット自身の分を除く
        users = [user async for user in reaction.users() if not user.bot]
        if users:
            import random
            winner = random.choice(users)
            
            winner_embed = discord.Embed(
                title="🎊 ギブアウェイ結果",
                description=f"**当選者:** {winner.mention}\n**賞品:** {prize}",
                color=0x00ff00
            )
            await interaction.followup.send(embed=winner_embed)
        else:
            await interaction.followup.send("参加者がいませんでした 😢")
    else:
        await interaction.followup.send("参加者がいませんでした 😢")

@tasks.loop(seconds=1)
async def check_scheduled_messages():
    """スケジュールされたメッセージをチェック"""
    current_time = datetime.now().time()
    current_time_str = current_time.strftime("%H:%M:%S")
    
    for schedule_id, schedule_data in list(scheduled_messages.items()):
        if (schedule_data['active'] and 
            schedule_data['time'].strftime("%H:%M:%S") == current_time_str):
            
            try:
                channel = bot.get_channel(schedule_data['channel_id'])
                if channel:
                    message = schedule_data['message']
                    if schedule_data['everyone']:
                        message = f"@everyone {message}"
                    
                    await channel.send(message)
                    # 一度実行したら削除
                    del scheduled_messages[schedule_id]
                    
            except Exception as e:
                print(f"スケジュールメッセージ送信エラー: {e}")

# ボット実行
if __name__ == "__main__":
    print("ボットとWebサーバーを開始中...")
    if TOKEN:
        # Flask サーバーを別スレッドで実行
        flask_thread = threading.Thread(target=run_flask)
        flask_thread.daemon = True
        flask_thread.start()
        
        # Discord bot を実行
        bot.run(TOKEN)
    else:
        print("DISCORD_BOT_TOKENが設定されていません。Secretsで環境変数を設定してください。")
