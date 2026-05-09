`timescale 1ns / 1ps
// ================================================================
//  uart_tx.v  -  Simple 8N1 UART Transmitter
//  Baud rate : 9600
//  Clock     : 100 MHz  →  divisor = 100_000_000 / 9600 = 10416
//
//  Usage:
//    - Put data on `tx_data[7:0]`
//    - Pulse `tx_start` HIGH for 1 clock cycle
//    - Wait until `tx_busy` goes LOW before sending next byte
//    - `tx` pin connects to Basys3 UART pin A18
// ================================================================

module uart_tx(
    input        clk,       // 100 MHz system clock
    input        rst,       // active HIGH reset
    input  [7:0] tx_data,   // byte to transmit
    input        tx_start,  // pulse high 1 cycle to begin
    output reg   tx,        // UART TX line
    output       tx_busy    // HIGH while transmitting
);

// ── Baud rate: 100MHz / 9600 = 10416 cycles per bit ─────────────
localparam CLKS_PER_BIT = 10416;

// ── FSM states ───────────────────────────────────────────────────
localparam IDLE      = 3'd0;
localparam START_BIT = 3'd1;
localparam DATA_BITS = 3'd2;
localparam STOP_BIT  = 3'd3;
localparam CLEANUP   = 3'd4;

reg [2:0]  state     = IDLE;
reg [13:0] clk_count = 0;      // counts up to CLKS_PER_BIT
reg [2:0]  bit_index = 0;      // which data bit we're sending
reg [7:0]  tx_shift  = 0;      // shift register holding the byte

assign tx_busy = (state != IDLE);

always @(posedge clk or posedge rst) begin
    if (rst) begin
        state     <= IDLE;
        tx        <= 1'b1;     // idle line is HIGH
        clk_count <= 0;
        bit_index <= 0;
        tx_shift  <= 0;
    end
    else begin
        case (state)

            // ── Wait for tx_start pulse ───────────────────────
            IDLE: begin
                tx        <= 1'b1;
                clk_count <= 0;
                bit_index <= 0;
                if (tx_start) begin
                    tx_shift <= tx_data;
                    state    <= START_BIT;
                end
            end

            // ── Send START bit (LOW for one bit period) ───────
            START_BIT: begin
                tx <= 1'b0;
                if (clk_count < CLKS_PER_BIT - 1) begin
                    clk_count <= clk_count + 1;
                end else begin
                    clk_count <= 0;
                    state     <= DATA_BITS;
                end
            end

            // ── Send 8 data bits, LSB first ───────────────────
            DATA_BITS: begin
                tx <= tx_shift[bit_index];
                if (clk_count < CLKS_PER_BIT - 1) begin
                    clk_count <= clk_count + 1;
                end else begin
                    clk_count <= 0;
                    if (bit_index < 7) begin
                        bit_index <= bit_index + 1;
                    end else begin
                        bit_index <= 0;
                        state     <= STOP_BIT;
                    end
                end
            end

            // ── Send STOP bit (HIGH for one bit period) ───────
            STOP_BIT: begin
                tx <= 1'b1;
                if (clk_count < CLKS_PER_BIT - 1) begin
                    clk_count <= clk_count + 1;
                end else begin
                    clk_count <= 0;
                    state     <= CLEANUP;
                end
            end

            // ── One idle cycle then back to IDLE ──────────────
            CLEANUP: begin
                state <= IDLE;
            end

            default: state <= IDLE;
        endcase
    end
end

endmodule