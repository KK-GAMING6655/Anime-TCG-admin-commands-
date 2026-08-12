import discord
from discord import app_commands, ui
import sqlite3
import os
import random
import asyncio
import urllib.request
import urllib.parse
from flask import Flask
from threading import Thread
import datetime
import libsql

# --- 1. WEB SERVER ---
app = Flask(__name__)
@app.route('/')
def home(): return "Bot is awake!"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

# --- 2. DATABASE SETUP ---
# --- SECTION 2: CLOUD DATABASE SETUP (TURSO) ---

# This pulls the secrets from Render safely
TURSO_URL = os.getenv("TURSO_URL")
TURSO_TOKEN = os.getenv("TURSO_TOKEN")

# Connect to the Cloud Database using the variables
conn = libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)
cursor = conn.cursor()

def init_db():
    # 1. Create all tables (Now living in the cloud!)
    cursor.execute('CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, balance INTEGER DEFAULT 0)')
    cursor.execute('CREATE TABLE IF NOT EXISTS cards (card_id TEXT PRIMARY KEY, name TEXT UNIQUE, rarity TEXT, value INTEGER, image TEXT)')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS inventory (
                        user_id TEXT, 
                        card_id TEXT, 
                        quantity INTEGER DEFAULT 1, 
                        UNIQUE(user_id, card_id))''')
    
    cursor.execute('CREATE TABLE IF NOT EXISTS rarities (name TEXT PRIMARY KEY, color TEXT, chance REAL)')
    cursor.execute('CREATE TABLE IF NOT EXISTS market (selling_id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id TEXT, card_id TEXT, price INTEGER, quantity INTEGER)')
    cursor.execute('CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)')

    # 2. AUTO-REPAIR: Ensure columns exist in the cloud
    cursor.execute("PRAGMA table_info(users)")
    existing_columns = [column[1] for column in cursor.fetchall()]
    
    if "account_status" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN account_status TEXT DEFAULT 'public'")
    if "last_beg" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN last_beg TIMESTAMP")
    if "last_daily" not in existing_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN last_daily TIMESTAMP")

    # 3. Default Settings
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('gacha_cost', '1000')")
    
    conn.commit()

init_db()


# --- 3. UTILITY FUNCTIONS ---

RARITY_ORDER = {
    'Common': 1, 
    'Uncommon': 2, 
    'Rare': 3, 
    'Epic': 4, 
    'Legendary': 5, 
    'Super Legendary': 6
}

def upload_to_catbox_sync(image_url: str) -> str:
    """Converts a Discord CDN image URL into a permanent Catbox link."""
    if "cdn.discordapp.com" in image_url or "media.discordapp.net" in image_url:
        catbox_url = "https://catbox.moe/user/api.php"
        data = urllib.parse.urlencode({
            "reqtype": "urlupload",
            "url": image_url
        }).encode("utf-8")
        
        req = urllib.request.Request(
            catbox_url, 
            data=data, 
            headers={"User-Agent": "Mozilla/5.0"}
        )
        
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                result = response.read().decode("utf-8").strip()
                if result.startswith("https://files.catbox.moe/"):
                    return result
        except Exception as e:
            print(f"Catbox upload failed: {e}")
            
    return image_url  # Fallback to original URL if not Discord CDN or upload fails


def get_user_stats(user_id):
    """Calculates rarity counts and total points for a user."""
    cursor.execute('''SELECT c.rarity, SUM(i.quantity) FROM inventory i 
                      JOIN cards c ON i.card_id = c.card_id WHERE i.user_id = ? GROUP BY c.rarity''', (str(user_id),))
    rows = cursor.fetchall()
    stats = {"Common": 0, "Uncommon": 0, "Rare": 0, "Epic": 0, "Legendary": 0, "Super Legendary": 0}
    for rarity, count in rows:
        if rarity in stats: stats[rarity] = count
    
    points = (stats["Common"] * 1) + (stats["Uncommon"] * 2) + (stats["Rare"] * 3) + \
             (stats["Epic"] * 4) + (stats["Legendary"] * 8) + (stats["Super Legendary"] * 10)
    return stats, points

def get_all_leaderboard_data():
    """Ranks all users based on points."""
    cursor.execute('SELECT DISTINCT user_id FROM inventory')
    user_ids = [row[0] for row in cursor.fetchall()]
    leaderboard = []
    for uid in user_ids:
        stats, points = get_user_stats(uid)
        leaderboard.append({"id": uid, "stats": stats, "points": points})
    # Sort by points descending
    leaderboard.sort(key=lambda x: x["points"], reverse=True)
    return leaderboard


# --- 4. UI CLASSES ---

class CardPaginator(ui.View):
    def __init__(self, cards, start_index, title_prefix="Card"):
        super().__init__(timeout=60)
        self.cards = cards
        self.current_page = start_index
        self.title_prefix = title_prefix

    def create_embed(self):
        card = self.cards[self.current_page]
        # card structure: (id, name, rarity, value, image)
        card_id, name, rarity, value, image = card[0], card[1], card[2], card[3], card[4]
        
        cursor.execute('SELECT color FROM rarities WHERE name = ?', (rarity,))
        res = cursor.fetchone()
        
        try:
            color = int(res[0].replace("#", ""), 16) if res else 0x3498db
        except:
            color = 0x3498db

        embed = discord.Embed(title=f"{self.title_prefix}", color=color)
        embed.description = f"**Page {self.current_page + 1} of {len(self.cards)}**"

        if "Collection" in self.title_prefix or "Inventory" in self.title_prefix:
            qty = card[5] if len(card) > 5 else 1
            info_text = f"**Rarity:** {rarity}\n**Value:** {value} 🪙\n**Quantity:** x{qty}\n**Card ID:** `{card_id}`"
        else:
            cursor.execute('SELECT COUNT(DISTINCT user_id) FROM inventory WHERE card_id = ?', (card_id,))
            owners_count = cursor.fetchone()[0]
            info_text = f"**Rarity:** {rarity}\n**Value:** {value} 🪙\n**Owners:** {owners_count} 👥\n**Card ID:** `{card_id}`"

        embed.add_field(name=f"**{name}**", value=info_text, inline=False)
        embed.set_image(url=image)
        return embed

    @ui.button(label="⬅️", style=discord.ButtonStyle.grey)
    async def prev(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else: await interaction.response.defer()

    @ui.button(label="➡️", style=discord.ButtonStyle.grey)
    async def next(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page < len(self.cards) - 1:
            self.current_page += 1
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else: await interaction.response.defer()


class DropView(ui.View):
    def __init__(self, card, quantity):
        super().__init__(timeout=None)
        self.card = card
        self.remaining = quantity

    @ui.button(label="Get", style=discord.ButtonStyle.green)
    async def get_card(self, interaction: discord.Interaction, button: ui.Button):
        if not hasattr(self, 'claimed_users'):
            self.claimed_users = []

        if interaction.user.id in self.claimed_users:
            return await interaction.response.send_message("❌ You have already claimed a card from this drop!", ephemeral=True)

        if self.remaining <= 0:
            return await interaction.response.send_message("All cards claimed!", ephemeral=True)
        
        cursor.execute('''INSERT INTO inventory (user_id, card_id, quantity) VALUES (?, ?, 1) 
                          ON CONFLICT(user_id, card_id) DO UPDATE SET quantity = quantity + 1''', 
                       (str(interaction.user.id), self.card[0]))
        conn.commit()
        
        self.claimed_users.append(interaction.user.id)
        self.remaining -= 1
        
        congrats_embed = discord.Embed(
            description=f"Congratulations 🎉 {interaction.user.mention} won **{self.card[1]} ({self.card[2]})** from the drop!",
            color=0xFFFF00 
        )
        await interaction.channel.send(embed=congrats_embed)

        if self.remaining <= 0:
            button.disabled, button.label = True, "Claimed Out"
            await interaction.message.edit(view=self)
        else:
            embed = interaction.message.embeds[0]
            embed.set_field_at(0, name=embed.fields[0].name, 
                               value=f"**Rarity:** {self.card[2]}\n**Value:** {self.card[3]} 🪙\n**Quantity Remaining:** {self.remaining}", 
                               inline=False)
            await interaction.message.edit(embed=embed, view=self)
        
        if not interaction.response.is_done():
            await interaction.response.defer()


class SaleView(ui.View):
    def __init__(self, seller, buyer, card, price, quantity):
        super().__init__(timeout=3600)
        self.seller, self.buyer, self.card, self.price, self.qty = seller, buyer, card, price, quantity

    @ui.button(label="✅ Accept", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        total = self.price * self.qty
        cursor.execute('SELECT balance FROM users WHERE id = ?', (str(self.buyer.id),))
        row = cursor.fetchone()
        if not row or row[0] < total:
            return await interaction.response.send_message(f"❌ Low balance! Need {total} 🪙", ephemeral=True)
        cursor.execute('UPDATE users SET balance = balance - ? WHERE id = ?', (total, str(self.buyer.id)))
        cursor.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (total, str(self.seller.id)))
        cursor.execute('UPDATE inventory SET quantity = quantity - ? WHERE user_id = ? AND card_id = ?', (self.qty, str(self.seller.id), self.card[0]))
        cursor.execute('INSERT INTO inventory (user_id, card_id, quantity) VALUES (?, ?, ?) ON CONFLICT(user_id, card_id) DO UPDATE SET quantity = quantity + ?', (str(self.buyer.id), self.card[0], self.qty, self.qty))
        cursor.execute('DELETE FROM inventory WHERE quantity <= 0')
        conn.commit()
        await interaction.response.send_message(f"✅ Bought {self.qty}x {self.card[1]}!")
        await self.seller.send(f"💰 {self.buyer.name} bought your cards for {total} 🪙!")
        self.stop()

    @ui.button(label="❌ Deny", style=discord.ButtonStyle.red)
    async def deny(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("Trade declined.")
        await self.seller.send(f"❌ {self.buyer.name} declined the offer.")
        self.stop()


class UserLeaderboardPaginator(ui.View):
    def __init__(self, data, start_index, client):
        super().__init__(timeout=60)
        self.data = data
        self.current_page = start_index
        self.client = client

    async def create_embed(self):
        user_data = self.data[self.current_page]
        user = self.client.get_user(int(user_data['id'])) or await self.client.fetch_user(int(user_data['id']))
        s = user_data['stats']
        
        embed = discord.Embed(title=f"Page {self.current_page + 1}/{len(self.data)}", color=0xFFFF00)
        embed.add_field(name=f"#{self.current_page + 1} **{user.name}**", value=(
            f"Common: {s['Common']}\nUncommon: {s['Uncommon']}\nRare: {s['Rare']}\n"
            f"Epic: {s['Epic']}\nLegendary: {s['Legendary']}\nSuper Legendary: {s['Super Legendary']}\n"
            f"**Collection Points: {user_data['points']}**"
        ), inline=False)
        return embed

    @ui.button(label="⬅️", style=discord.ButtonStyle.grey)
    async def prev(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=await self.create_embed(), view=self)
        else: await interaction.response.defer()

    @ui.button(label="➡️", style=discord.ButtonStyle.grey)
    async def next(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page < len(self.data) - 1:
            self.current_page += 1
            await interaction.response.edit_message(embed=await self.create_embed(), view=self)
        else: await interaction.response.defer()


class TradeView(ui.View):
    def __init__(self, sender, receiver, sender_card, receiver_card):
        super().__init__(timeout=120)
        self.sender = sender
        self.receiver = receiver
        self.sender_card = sender_card
        self.receiver_card = receiver_card
        self.accepted = False

    @ui.button(label="Accept Trade", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.receiver.id:
            return await interaction.response.send_message("Only the trade receiver can accept this!", ephemeral=True)
        
        cursor.execute('UPDATE inventory SET quantity = quantity - 1 WHERE user_id = ? AND card_id = ?', (str(self.sender.id), self.sender_card[0]))
        cursor.execute('INSERT INTO inventory (user_id, card_id, quantity) VALUES (?, ?, 1) ON CONFLICT(user_id, card_id) DO UPDATE SET quantity = quantity + 1', (str(self.receiver.id), self.sender_card[0]))
        
        cursor.execute('UPDATE inventory SET quantity = quantity - 1 WHERE user_id = ? AND card_id = ?', (str(self.receiver.id), self.receiver_card[0]))
        cursor.execute('INSERT INTO inventory (user_id, card_id, quantity) VALUES (?, ?, 1) ON CONFLICT(user_id, card_id) DO UPDATE SET quantity = quantity + 1', (str(self.sender.id), self.receiver_card[0]))
        cursor.execute('DELETE FROM inventory WHERE quantity <= 0')
        conn.commit()

        self.accepted = True
        self.stop()
        await interaction.response.edit_message(content=f"🤝 **Trade Complete!** {self.sender.mention} and {self.receiver.mention} have swapped cards.", view=None)

    @ui.button(label="Decline", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id not in [self.sender.id, self.receiver.id]:
            return await interaction.response.send_message("This isn't your trade!", ephemeral=True)
        self.stop()
        await interaction.response.edit_message(content="❌ Trade cancelled.", view=None)


class MarketPaginator(ui.View):
    def __init__(self, listings, client):
        super().__init__(timeout=120)
        self.listings = listings
        self.current_page = 0
        self.client = client
        
        self.remove_item(self.btn_confirm)
        self.remove_item(self.btn_cancel)

    async def create_embed(self):
        item = self.listings[self.current_page]
        selling_id, seller_id, price, qty = item[0], item[1], item[2], item[3]
        card_id, name, rarity, value, image = item[4], item[5], item[6], item[7], item[8]
        total_amount = price * qty

        cursor.execute('SELECT color FROM rarities WHERE name = ?', (rarity,))
        res = cursor.fetchone()
        color = int(res[0].replace("#", ""), 16) if res else 0x3498db

        cursor.execute('SELECT COUNT(DISTINCT user_id) FROM inventory WHERE card_id = ?', (card_id,))
        owners = cursor.fetchone()[0]

        try:
            seller = self.client.get_user(int(seller_id)) or await self.client.fetch_user(int(seller_id))
            seller_name = seller.name
        except:
            seller_name = "Unknown User"

        embed = discord.Embed(title="🛒 Global Market", color=color)
        embed.description = f"**Page {self.current_page + 1} of {len(self.listings)}**"
        embed.add_field(name=f"**{name}**", value=(
            f"**Rarity:** {rarity}\n"
            f"**Value:** {value} 🪙\n"
            f"**Owners:** {owners} 👥\n"
            f"**Selling Amount:** {price} 🪙\n"
            f"**Quantity:** {qty}\n"
            f"**Total Amount:** {total_amount} 🪙\n"
            f"**Seller:** {seller_name}\n"
            f"**Card ID:** `{card_id}`"
        ), inline=False)
        embed.set_image(url=image)
        embed.set_footer(text=f"Selling ID: {selling_id}")
        return embed

    @ui.button(label="⬅️", style=discord.ButtonStyle.grey, custom_id="prev")
    async def btn_prev(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=await self.create_embed(), view=self)
        else: await interaction.response.defer()

    @ui.button(label="Buy", style=discord.ButtonStyle.green, custom_id="buy")
    async def btn_buy(self, interaction: discord.Interaction, button: ui.Button):
        self.remove_item(self.btn_prev)
        self.remove_item(self.btn_buy)
        self.remove_item(self.btn_next)
        self.add_item(self.btn_confirm)
        self.add_item(self.btn_cancel)
        await interaction.response.edit_message(view=self)

    @ui.button(label="➡️", style=discord.ButtonStyle.grey, custom_id="next")
    async def btn_next(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page < len(self.listings) - 1:
            self.current_page += 1
            await interaction.response.edit_message(embed=await self.create_embed(), view=self)
        else: await interaction.response.defer()

    @ui.button(label="Confirm", style=discord.ButtonStyle.green, custom_id="confirm")
    async def btn_confirm(self, interaction: discord.Interaction, button: ui.Button):
        item = self.listings[self.current_page]
        selling_id, seller_id, price, qty = item[0], item[1], item[2], item[3]
        card_id, name, rarity, value, image = item[4], item[5], item[6], item[7], item[8]
        total_amount = price * qty

        cursor.execute('SELECT * FROM market WHERE selling_id = ?', (selling_id,))
        if not cursor.fetchone():
            await interaction.response.send_message(embed=discord.Embed(description="⚠️ This item was already sold or removed!", color=discord.Color.red()), ephemeral=True)
            try: await interaction.message.delete()
            except: pass
            return

        if str(interaction.user.id) == str(seller_id):
            return await interaction.response.send_message(embed=discord.Embed(description="⚠️ You cannot buy your own listing!", color=discord.Color.red()), ephemeral=True)

        cursor.execute('SELECT balance FROM users WHERE id = ?', (str(interaction.user.id),))
        row = cursor.fetchone()
        balance = row[0] if row else 0

        if balance < total_amount:
            err_embed = discord.Embed(description=f"{interaction.user.mention}, you don't have enough balance to buy that item.\n**Your balance:** {balance} 🪙", color=discord.Color.red())
            await interaction.response.send_message(embed=err_embed, ephemeral=True)
            try: await interaction.message.delete()
            except: pass
            return

        cursor.execute('UPDATE users SET balance = balance - ? WHERE id = ?', (total_amount, str(interaction.user.id)))
        cursor.execute('INSERT INTO users (id, balance) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET balance = balance + ?', (str(seller_id), total_amount, total_amount))
        cursor.execute('DELETE FROM market WHERE selling_id = ?', (selling_id,))
        cursor.execute('INSERT INTO inventory (user_id, card_id, quantity) VALUES (?, ?, ?) ON CONFLICT(user_id, card_id) DO UPDATE SET quantity = quantity + ?', (str(interaction.user.id), card_id, qty, qty))
        conn.commit()

        pub_embed = discord.Embed(description=f"🎉 {interaction.user.mention} bought **{name} ({rarity})** from the market for **{total_amount}** 🪙.", color=discord.Color.green())
        pub_embed.add_field(name="Card Details", value=f"**Card Name:** {name}\n**Rarity:** {rarity}\n**Value:** {value}\n**Card Id:** `{card_id}`\n**Quantity:** {qty}\n**Amount:** {total_amount} 🪙", inline=False)
        pub_embed.set_image(url=image)
        
        await interaction.channel.send(embed=pub_embed)
        await interaction.response.send_message("✅ Purchase successful!", ephemeral=True)
        try: await interaction.message.delete()
        except: pass

    @ui.button(label="Cancel", style=discord.ButtonStyle.red, custom_id="cancel")
    async def btn_cancel(self, interaction: discord.Interaction, button: ui.Button):
        self.remove_item(self.btn_confirm)
        self.remove_item(self.btn_cancel)
        self.add_item(self.btn_prev)
        self.add_item(self.btn_buy)
        self.add_item(self.btn_next)
        await interaction.response.edit_message(view=self)


class HelpPaginator(ui.View):
    def __init__(self, pages):
        super().__init__(timeout=60)
        self.pages = pages
        self.current_page = 0

    def create_embed(self):
        embed = discord.Embed(title="📜 Bot Help Menu", color=0xFFFF00)
        embed.description = f"**Page {self.current_page + 1} of {len(self.pages)}**\n\n{self.pages[self.current_page]}"
        return embed

    @ui.button(label="⬅️", style=discord.ButtonStyle.grey)
    async def prev(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else: await interaction.response.defer()

    @ui.button(label="➡️", style=discord.ButtonStyle.grey)
    async def next(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else: await interaction.response.defer()

# --- 5. BOT SETUP ---
class GachaBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("Slash commands synced via setup_hook")

client = GachaBot()

@client.event
async def on_ready():
    print(f'Logged in as {client.user} (ID: {client.user.id})')
    print('------')
    try:
        synced = await client.tree.sync()
        print(f"Synced {len(synced)} command(s) successfully!")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


@client.event
async def on_message(message):
    if message.author.bot: return
    c = random.randint(10, 50)
    cursor.execute('INSERT INTO users (id, balance) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET balance = balance + ?', (str(message.author.id), c, c))
    conn.commit()


# --- 6. COMMANDS ---

@client.tree.command(name="add_card", description="Admin: Add card")
async def add_card(interaction: discord.Interaction, name: str, rarity: str, value: int, image_url: str):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.manage_guild: 
        return await interaction.followup.send("❌ Admin!")
    
    # Automatically convert Discord CDN link (or web link) to a permanent Catbox link
    final_image_url = await asyncio.to_thread(upload_to_catbox_sync, image_url)
    
    new_id = random.randint(100000, 999999)
    cursor.execute('INSERT INTO cards (card_id, name, rarity, value, image) VALUES (?, ?, ?, ?, ?)', (new_id, name, rarity, value, final_image_url))
    conn.commit()
    await interaction.followup.send(f"✅ Added **{name}** (ID: `{new_id}`)\n🖼️ Image URL: {final_image_url}")


@client.tree.command(name="addcoin", description="Admin: Give coins to a user")
async def addcoin(interaction: discord.Interaction, user: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.manage_guild: 
        return await interaction.followup.send("❌ Admin only!")
    
    cursor.execute('INSERT INTO users (id, balance) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET balance = balance + ?', 
                   (str(user.id), amount, amount))
    conn.commit()
    await interaction.followup.send(f"✅ Added **{amount}** coins to {user.mention}!")


@client.tree.command(name="inspect_inventory", description="Admin: View another user's collection")
async def inspect_inventory(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.manage_guild: 
        return await interaction.followup.send("❌ Admin only!")
    
    cursor.execute('SELECT c.*, i.quantity FROM inventory i JOIN cards c ON i.card_id = c.card_id WHERE i.user_id = ?', (str(user.id),))
    items = cursor.fetchall()
    if not items: 
        return await interaction.followup.send(f"❌ {user.name}'s inventory is empty.")
    
    view = CardPaginator(items, 0, f"{user.name}'s Collection")
    await interaction.followup.send(embed=view.create_embed(), view=view)


@client.tree.command(name="drop", description="Admin: Public card drop")
async def drop(interaction: discord.Interaction, name: str, quantity: int):
    if not interaction.user.guild_permissions.manage_guild: return await interaction.response.send_message("❌ Admin only!")
    cursor.execute('SELECT * FROM cards WHERE name = ? OR card_id = ?', (name, name))
    card = cursor.fetchone()
    if not card: return await interaction.response.send_message("Card not found!")
    embed = discord.Embed(title="🎁 PUBLIC DROP!", color=discord.Color.gold())
    embed.add_field(name=f"**{card[1]}**", value=f"Rarity:\nValue: 🪙\n**Quantity Remaining:** {quantity}", inline=False)
    embed.set_image(url=card[4])
    await interaction.channel.send(embed=embed, view=DropView(card, quantity))
    await interaction.response.send_message("Drop sent!", ephemeral=True)


# --- PART 7: ADMIN UTILITIES ---

@client.tree.command(name="set_channel", description="Admin: Set the default channel for bot announcements")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild: 
        return await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)

    cursor.execute('''INSERT INTO config (key, value) VALUES (?, ?) 
                      ON CONFLICT(key) DO UPDATE SET value = ?''', 
                   ('default_channel', str(channel.id), str(channel.id)))
    conn.commit()
    
    embed = discord.Embed(description=f"✅ Default announcement channel successfully set to {channel.mention}", color=discord.Color.green())
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="clear_balance", description="Admin: Reset a user's coin balance to 0")
async def clear_balance(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.manage_guild: 
        return await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
    
    cursor.execute('UPDATE users SET balance = 0 WHERE id = ?', (str(user.id),))
    conn.commit()
    
    embed = discord.Embed(description=f"✅ Successfully cleared {user.mention}'s coin balance to 0 🪙.", color=discord.Color.green())
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="clear_inventory", description="Admin: Remove all cards from a user's inventory")
async def clear_inventory(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.manage_guild: 
        return await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
    
    cursor.execute('DELETE FROM inventory WHERE user_id = ?', (str(user.id),))
    conn.commit()
    
    embed = discord.Embed(description=f"✅ Successfully emptied {user.mention}'s card inventory.", color=discord.Color.green())
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- PART 8: ADMIN MANAGEMENT COMMANDS ---

@client.tree.command(name="delete_card", description="Admin: Delete a card completely from the game")
async def delete_card(interaction: discord.Interaction, card_name: str):
    if not interaction.user.guild_permissions.manage_guild: 
        return await interaction.response.send_message("❌ Admin only!", ephemeral=True)
    
    cursor.execute('SELECT card_id, name FROM cards WHERE name = ? OR card_id = ?', (card_name, card_name))
    card = cursor.fetchone()
    if not card: 
        return await interaction.response.send_message("❌ Card not found.", ephemeral=True)
    
    card_id, real_name = card[0], card[1]
    
    cursor.execute('DELETE FROM cards WHERE card_id = ?', (card_id,))
    cursor.execute('DELETE FROM inventory WHERE card_id = ?', (card_id,))
    cursor.execute('DELETE FROM market WHERE card_id = ?', (card_id,))
    conn.commit()
    
    await interaction.response.send_message(f"✅ Card **{real_name}** has been permanently deleted from the database, all inventories, and the market.", ephemeral=True)


@client.tree.command(name="remove_coin", description="Admin: Remove coins from a user")
async def remove_coin(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not interaction.user.guild_permissions.manage_guild: 
        return await interaction.response.send_message("❌ Admin only!", ephemeral=True)
    
    cursor.execute('SELECT balance FROM users WHERE id = ?', (str(user.id),))
    row = cursor.fetchone()
    balance = row[0] if row else 0
    
    if balance < amount:
        err_embed = discord.Embed(description=f"{user.mention} doesn't have enough coin to remove.\n**Balance: {balance} 🪙", color=discord.Color.red())
        return await interaction.response.send_message(embed=err_embed)
    
    cursor.execute('UPDATE users SET balance = balance - ? WHERE id = ?', (amount, str(user.id)))
    conn.commit()
    
    await interaction.response.send_message(f"✅ Successfully removed {amount} 🪙 from {user.mention}.", ephemeral=True)


@client.tree.command(name="remove_card", description="Admin: Remove specific cards from a user")
async def remove_card(interaction: discord.Interaction, user: discord.Member, card_name: str, quantity: int):
    if not interaction.user.guild_permissions.manage_guild: 
        return await interaction.response.send_message("❌ Admin only!", ephemeral=True)
    
    cursor.execute('''SELECT c.card_id, i.quantity, c.name FROM inventory i 
                      JOIN cards c ON i.card_id = c.card_id 
                      WHERE i.user_id = ? AND (c.name = ? OR c.card_id = ?)''', 
                   (str(user.id), card_name, card_name))
    card = cursor.fetchone()
    
    if not card:
        embed = discord.Embed(description=f"{user.mention} doesn't have that card to remove.", color=discord.Color.red())
        return await interaction.response.send_message(embed=embed)
        
    if card[1] < quantity:
        embed = discord.Embed(description=f"{user.mention} doesn't have enough card to remove.", color=discord.Color.red())
        return await interaction.response.send_message(embed=embed)
        
    cursor.execute('UPDATE inventory SET quantity = quantity - ? WHERE user_id = ? AND card_id = ?', (quantity, str(user.id), card[0]))
    cursor.execute('DELETE FROM inventory WHERE quantity <= 0')
    conn.commit()
    
    await interaction.response.send_message(f"✅ Removed {quantity}x ** from {user.mention}'s inventory.", ephemeral=True)


@client.tree.command(name="remove_rarity", description="Admin: Remove a rarity tier")
async def remove_rarity(interaction: discord.Interaction, rarity: str):
    if not interaction.user.guild_permissions.manage_guild: 
        return await interaction.response.send_message("❌ Admin only!", ephemeral=True)
    
    cursor.execute('DELETE FROM rarities WHERE name = ?', (rarity,))
    if cursor.rowcount == 0:
        return await interaction.response.send_message(f"❌ Rarity **{rarity}** not found.", ephemeral=True)
        
    cursor.execute('UPDATE cards SET rarity = "Unknown" WHERE rarity = ?', (rarity,))
    conn.commit()
    
    await interaction.response.send_message(f"✅ Rarity **{rarity} removed. Any affected cards now have 'Unknown' rarity.", ephemeral=True)


@client.tree.command(name="edit", description="Admin: Edit an existing card's details")
async def edit(interaction: discord.Interaction, card_name: str, new_name: str = None, rarity: str = None, value: int = None, image: str = None):
    if not interaction.user.guild_permissions.manage_guild: 
        return await interaction.response.send_message("❌ Admin only!", ephemeral=True)
    
    cursor.execute('SELECT card_id, name, rarity, value, image FROM cards WHERE name = ? OR card_id = ?', (card_name, card_name))
    card = cursor.fetchone()
    
    if not card: 
        return await interaction.response.send_message("❌ Card not found.", ephemeral=True)
    
    card_id = card[0]
    
    # If a new image URL is supplied, convert it to Catbox if needed
    final_image = card[4]
    if image:
        final_image = await asyncio.to_thread(upload_to_catbox_sync, image)

    final_name = new_name if new_name else card[1]
    final_rarity = rarity if rarity else card[2]
    final_value = value if value is not None else card[3]
    
    try:
        cursor.execute('UPDATE cards SET name = ?, rarity = ?, value = ?, image = ? WHERE card_id = ?', 
                       (final_name, final_rarity, final_value, final_image, card_id))
        conn.commit()
        await interaction.response.send_message(f"✅ Card ** updated successfully!", ephemeral=True)
    except sqlite3.IntegrityError:
        await interaction.response.send_message("❌ A card with that new name already exists!", ephemeral=True)


# --- PART 9: FINAL FEATURES ---

@client.tree.command(name="add_rarity", description="Admin: Add a new rarity tier")
async def add_rarity(interaction: discord.Interaction, name: str, drop_rate: float, colour: str):
    if not interaction.user.guild_permissions.manage_guild: return await interaction.response.send_message("❌ Admin only!", ephemeral=True)
    if not (0 < drop_rate < 100): return await interaction.response.send_message("❌ Drop rate must be between 0 and 100 (exclusive)!", ephemeral=True)
    
    cursor.execute('INSERT INTO rarities (name, chance, color) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET chance = ?, color = ?', (name, drop_rate, colour, drop_rate, colour))
    conn.commit()
    await interaction.response.send_message(f"✅ Rarity **{name}** added with **{drop_rate}%** drop rate.", ephemeral=True)


@client.tree.command(name="luck_amount", description="Admin: Set the gacha pull cost")
async def luck_amount(interaction: discord.Interaction, amount: int):
    if not interaction.user.guild_permissions.manage_guild: return await interaction.response.send_message("❌ Admin only!", ephemeral=True)
    cursor.execute("UPDATE config SET value = ? WHERE key = 'gacha_cost'", (str(amount),))
    conn.commit()
    await interaction.response.send_message(f"✅ Gacha cost updated to **{amount} 🪙**.", ephemeral=True)


@client.tree.command(name="user_balance", description="Check another member's balance")
async def user_balance(interaction: discord.Interaction, user: discord.Member):
    cursor.execute('SELECT balance, account_status FROM users WHERE id = ?', (str(user.id),))
    row = cursor.fetchone()
    if row and row[1] == 'private' and interaction.user.id != user.id:
        embed = discord.Embed(description=f"❌ {user.mention}'s account is private.\nYou can't get details of that account.", color=discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    
    bal = row[0] if row else 0
    embed = discord.Embed(title=f"{user.name}'s balance", description=f"**Balance:** {bal} 🪙", color=0xFFFF00)
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="user_inventory", description="Check another member's inventory")
async def user_inventory(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer()
    
    cursor.execute('SELECT account_status FROM users WHERE id = ?', (str(user.id),))
    row = cursor.fetchone()
    
    if row and row[0] == 'private' and interaction.user.id != user.id:
        embed = discord.Embed(description=f"❌ {user.mention}'s account is private.\nYou can't get details of that account.", color=discord.Color.red())
        return await interaction.followup.send(embed=embed)
    
    cursor.execute('''SELECT c.card_id, c.name, c.rarity, c.value, c.image, i.quantity 
                      FROM inventory i 
                      JOIN cards c ON i.card_id = c.card_id 
                      WHERE i.user_id = ?''', (str(user.id),))
    cards = cursor.fetchall()
    
    if not cards:
        return await interaction.followup.send(f"{user.name} does not have any cards yet!")
    
    view = CardPaginator(cards, 0, f"{user.name}'s Inventory")
    await interaction.followup.send(embed=view.create_embed(), view=view)


@client.tree.command(name="migrate_images", description="One-time: move all card images to imgbb (permanent hosting)")
async def migrate_images(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Admins only.", ephemeral=True)

    await interaction.response.defer()
    local_cursor = conn.cursor()
    local_cursor.execute("SELECT card_id, name, image FROM cards")
    all_cards = local_cursor.fetchall()

    IMGBB_API_KEY = "aaa1efad377f06cbed2acfb889a045ba"
    success, failed = 0, []

    for card_id, name, image_url in all_cards:
        try:
            def upload(url):
                data = urllib.parse.urlencode({
                    "key": IMGBB_API_KEY,
                    "image": url
                }).encode("utf-8")
                req = urllib.request.Request(
                    "https://api.imgbb.com/1/upload",
                    data=data,
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    import json
                    result = json.loads(resp.read().decode("utf-8"))
                    return result["data"]["url"]

            new_url = await asyncio.to_thread(upload, image_url)

            local_cursor.execute("UPDATE cards SET image = ? WHERE card_id = ?", (new_url, card_id))
            conn.commit()
            success += 1

        except Exception as e:
            failed.append(f"{name} ({card_id}): {e}")

        await asyncio.sleep(1)

    result = f"✅ Migrated {success}/{len(all_cards)} cards to imgbb."
    if failed:
        result += f"\n\n❌ Failed ({len(failed)}):\n" + "\n".join(failed[:15])
        if len(failed) > 15:
            result += f"\n...and {len(failed) - 15} more."

    await interaction.followup.send(result[:2000])


if __name__ == '__main__':
    Thread(target=run_flask).start()
    client.run(os.environ.get('DISCORD_TOKEN'))

