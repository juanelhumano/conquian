import random
import string
from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secreto_conquian_123'
socketio = SocketIO(app, cors_allowed_origins="*")

# Estructura de datos en memoria para las salas
# rooms = {
#     "CODE": {
#         "players": [{"id": "sid", "alias": "Nombre", "hand": []}],
#         "deck": [],
#         "discard_pile": [],
#         "turn_index": 0,
#         "started": False,
#         "melds": {} # Cartas bajadas a la mesa
#     }
# }
rooms = {}

def create_spanish_deck():
    """Crea una baraja española de 40 cartas."""
    suits = ['Oros', 'Copas', 'Espadas', 'Bastos']
    ranks = [1, 2, 3, 4, 5, 6, 7, 10, 11, 12] # Sota=10, Caballo=11, Rey=12
    deck = []
    for suit in suits:
        for rank in ranks:
            deck.append({"suit": suit, "rank": rank, "id": f"{rank}-{suit}"})
    random.shuffle(deck)
    return deck

def generate_room_code():
    """Genera un código de sala aleatorio de 4 letras."""
    return ''.join(random.choices(string.ascii_uppercase, k=4))

@socketio.on('create_room')
def handle_create_room(data):
    alias = data.get('alias')
    room_code = generate_room_code()
    
    rooms[room_code] = {
        "players": [{"id": request.sid, "alias": alias, "hand": []}],
        "deck": create_spanish_deck(),
        "discard_pile": [],
        "turn_index": 0,
        "started": False,
        "melds": {} # {sid: [[carta, carta], [carta, carta]]}
    }
    
    join_room(room_code)
    emit('room_created', {'roomCode': room_code, 'alias': alias})
    emit('chat_message', {'sender': 'Sistema', 'msg': f'Sala {room_code} creada por {alias}.'}, room=room_code)

@socketio.on('join_room')
def handle_join_room(data):
    room_code = data.get('roomCode').upper()
    alias = data.get('alias')
    
    if room_code not in rooms:
        emit('error', {'msg': 'La sala no existe.'})
        return
    
    room = rooms[room_code]
    
    if len(room['players']) >= 2:
        emit('error', {'msg': 'La sala está llena.'})
        return
        
    room['players'].append({"id": request.sid, "alias": alias, "hand": []})
    join_room(room_code)
    
    emit('player_joined', {'alias': alias}, room=room_code)
    emit('chat_message', {'sender': 'Sistema', 'msg': f'{alias} se ha unido a la sala.'}, room=room_code)
    
    # Si hay 2 jugadores, iniciar juego automáticamente o esperar botón
    if len(room['players']) == 2:
        start_game(room_code)

def start_game(room_code):
    room = rooms[room_code]
    room['started'] = True
    deck = room['deck']
    
    # Repartir 9 cartas al primero, 8 al segundo (regla simple)
    # O repartir 8 a cada uno. Vamos a dar 8 a cada uno.
    for player in room['players']:
        player['hand'] = [deck.pop() for _ in range(8)]
        room['melds'][player['id']] = []
        
        # Enviar mano individual a cada jugador
        emit('game_start', {
            'hand': player['hand'],
            'turn': room['players'][room['turn_index']]['id']
        }, room=player['id'])

    # Carta inicial al pozo de descarte
    if deck:
        initial_card = deck.pop()
        room['discard_pile'].append(initial_card)

    update_game_state(room_code)

def update_game_state(room_code):
    """Envía el estado público del juego a todos en la sala."""
    room = rooms[room_code]
    top_discard = room['discard_pile'][-1] if room['discard_pile'] else None
    current_turn_id = room['players'][room['turn_index']]['id']
    
    state = {
        'topDiscard': top_discard,
        'deckCount': len(room['deck']),
        'turnId': current_turn_id,
        'melds': room['melds'] # Mostrar juegos bajados
    }
    emit('update_state', state, room=room_code)

@socketio.on('send_message')
def handle_message(data):
    room_code = data.get('roomCode')
    msg = data.get('message')
    alias = data.get('alias')
    if room_code:
        emit('chat_message', {'sender': alias, 'msg': msg}, room=room_code)

# --- Lógica de Juego Básica ---

@socketio.on('draw_card')
def handle_draw(data):
    room_code = data.get('roomCode')
    source = data.get('source') # 'deck' o 'discard'
    room = rooms.get(room_code)
    
    if not room: return

    # Verificar turno
    current_player = room['players'][room['turn_index']]
    if current_player['id'] != request.sid:
        return

    card = None
    if source == 'deck' and room['deck']:
        card = room['deck'].pop()
    elif source == 'discard' and room['discard_pile']:
        card = room['discard_pile'].pop()
    
    if card:
        current_player['hand'].append(card)
        emit('card_drawn', {'card': card}, room=request.sid) # Solo el jugador ve lo que robó
        update_game_state(room_code)

@socketio.on('discard_card')
def handle_discard(data):
    room_code = data.get('roomCode')
    card_id = data.get('cardId')
    room = rooms.get(room_code)
    
    if not room: return
    
    current_player = room['players'][room['turn_index']]
    if current_player['id'] != request.sid:
        return

    # Buscar y remover carta de la mano
    card_to_discard = None
    for i, card in enumerate(current_player['hand']):
        if card['id'] == card_id:
            card_to_discard = current_player['hand'].pop(i)
            break
            
    if card_to_discard:
        room['discard_pile'].append(card_to_discard)
        # Cambiar turno
        room['turn_index'] = (room['turn_index'] + 1) % len(room['players'])
        emit('hand_updated', {'hand': current_player['hand']}, room=request.sid)
        update_game_state(room_code)

@socketio.on('disconnect')
def handle_disconnect():
    # Lógica simple de limpieza. En prod debería manejar reconexiones.
    print(f"Cliente desconectado: {request.sid}")

if __name__ == '__main__':
    socketio.run(app, debug=True)
