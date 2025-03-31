# app.py
from flask import Flask, request, jsonify, render_template
import chess
import chess.pgn
import sqlite3
import os
from datetime import datetime
import json

app = Flask(__name__)

# Database setup
DB_PATH = "chess_games.db"

def init_db():
    """Initialize the database if it doesn't exist"""
    if not os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Create games table
        cursor.execute('''
        CREATE TABLE games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT NOT NULL,
            end_time TEXT,
            winner TEXT,
            pgn TEXT
        )
        ''')
        
        # Create moves table
        cursor.execute('''
        CREATE TABLE moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            move_number INTEGER NOT NULL,
            move_san TEXT NOT NULL,
            move_uci TEXT NOT NULL,
            fen_after TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (game_id) REFERENCES games (id)
        )
        ''')
        
        # Initialize first game
        cursor.execute('''
        INSERT INTO games (start_time, pgn) VALUES (?, ?)
        ''', (datetime.now().isoformat(), ""))
        
        conn.commit()
        conn.close()

# Initialize the database
init_db()

# Game state (in-memory)
board = chess.Board()
current_game_id = 1

def get_current_game_id():
    """Get the ID of the current active game"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(id) FROM games")
    game_id = cursor.fetchone()[0]
    conn.close()
    return game_id if game_id else 1

def start_new_game():
    """Start a new game in the database and reset the board"""
    global board
    global current_game_id
    
    # Create new game in DB
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO games (start_time, pgn) VALUES (?, ?)
    ''', (datetime.now().isoformat(), ""))
    
    current_game_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    # Reset the board
    board = chess.Board()
    
    return current_game_id

# Initialize current game ID
current_game_id = get_current_game_id()

# Recover board state if app was restarted
def recover_board_state():
    global board
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get the most recent game
    cursor.execute("SELECT id FROM games ORDER BY id DESC LIMIT 1")
    game_id = cursor.fetchone()[0]
    
    # Get the most recent position
    cursor.execute("SELECT fen_after FROM moves WHERE game_id = ? ORDER BY move_number DESC LIMIT 1", (game_id,))
    result = cursor.fetchone()
    
    if result:
        # Restore the board from the last known position
        board = chess.Board(result[0])
    else:
        # Start a new board
        board = chess.Board()
    
    conn.close()
    return game_id

# Recover board state at startup
current_game_id = recover_board_state()

@app.route('/')
def index():
    """Render the chess game UI"""
    return render_template('index.html')

@app.route('/api/game', methods=['GET'])
def get_game():
    """Get the current game state"""
    global board
    
    # Get the move history for display
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT move_number, move_san FROM moves WHERE game_id = ? ORDER BY move_number", 
                  (current_game_id,))
    moves = cursor.fetchall()
    conn.close()
    
    # Format move history for display
    formatted_moves = []
    for i in range(0, len(moves), 2):
        move_number = i // 2 + 1
        move_pair = f"{move_number}. {moves[i][1]}"
        if i + 1 < len(moves):
            move_pair += f" {moves[i+1][1]}"
        formatted_moves.append(move_pair)
    
    return jsonify({
        'fen': board.fen(),
        'game_id': current_game_id,
        'legal_moves': [move.uci() for move in board.legal_moves],
        'is_check': board.is_check(),
        'is_checkmate': board.is_checkmate(),
        'is_stalemate': board.is_stalemate(),
        'is_game_over': board.is_game_over(),
        'turn': 'white' if board.turn else 'black',
        'move_history': formatted_moves
    })

@app.route('/api/move', methods=['POST'])
def make_move():
    """Make a move in the chess game"""
    global board
    global current_game_id
    
    data = request.json
    move_uci = data.get('move')
    
    if not move_uci:
        return jsonify({'error': 'No move provided'}), 400
    
    try:
        # Convert UCI to a chess.Move object
        move = chess.Move.from_uci(move_uci)
        
        # Verify the move is legal
        if move not in board.legal_moves:
            return jsonify({'error': 'Illegal move'}), 400
        
        # Get move in SAN notation for logging
        move_san = board.san(move)
        
        # Make the move
        board.push(move)
        
        # Log the move
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Determine move number
        move_number = len(board.move_stack)
        
        cursor.execute('''
        INSERT INTO moves (game_id, move_number, move_san, move_uci, fen_after, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
        ''', (current_game_id, move_number, move_san, move_uci, board.fen(), datetime.now().isoformat()))
        
        # Check if game is over
        game_over = False
        winner = None
        
        if board.is_game_over():
            game_over = True
            if board.is_checkmate():
                winner = "Black" if board.turn else "White"
            else:
                winner = "Draw"
                
            # Update game record
            cursor.execute('''
            UPDATE games SET end_time = ?, winner = ?, pgn = ?
            WHERE id = ?
            ''', (
                datetime.now().isoformat(),
                winner,
                get_pgn_from_moves(current_game_id),
                current_game_id
            ))
            
            # Start a new game if the current one is over
            if game_over:
                start_new_game()
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'move': move_uci,
            'fen': board.fen(),
            'is_check': board.is_check(),
            'is_checkmate': board.is_checkmate(),
            'is_stalemate': board.is_stalemate(),
            'is_game_over': game_over,
            'winner': winner,
            'turn': 'white' if board.turn else 'black'
        })
        
    except ValueError:
        return jsonify({'error': 'Invalid move format'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/games', methods=['GET'])
def get_games():
    """Get a list of completed games"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
    SELECT id, start_time, end_time, winner, pgn 
    FROM games 
    WHERE end_time IS NOT NULL
    ORDER BY id DESC
    LIMIT 10
    ''')
    
    games = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return jsonify(games)

def get_pgn_from_moves(game_id):
    """Generate PGN from move history"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get game info
    cursor.execute("SELECT start_time, end_time, winner FROM games WHERE id = ?", (game_id,))
    game_info = cursor.fetchone()
    if not game_info:
        conn.close()
        return ""
    
    start_time, end_time, winner = game_info
    
    # Get moves in UCI format
    cursor.execute("SELECT move_uci FROM moves WHERE game_id = ? ORDER BY move_number", (game_id,))
    moves = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    # Create a new game and add all moves
    game = chess.pgn.Game()
    
    # Set headers
    game.headers["Event"] = "Community Chess Game"
    game.headers["Site"] = "Web App"
    game.headers["Date"] = datetime.fromisoformat(start_time).strftime("%Y.%m.%d")
    game.headers["Round"] = "1"
    game.headers["White"] = "Community"
    game.headers["Black"] = "Community"
    
    if winner:
        if winner == "White":
            game.headers["Result"] = "1-0"
        elif winner == "Black":
            game.headers["Result"] = "0-1"
        else:
            game.headers["Result"] = "1/2-1/2"
    
    # Add moves
    node = game
    temp_board = chess.Board()
    for uci_move in moves:
        move = chess.Move.from_uci(uci_move)
        node = node.add_variation(move)
        temp_board.push(move)
    
    # Return the PGN as a string
    return str(game)

if __name__ == '__main__':
    app.run(debug=True)