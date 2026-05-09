`timescale 1ns / 1ps

module floor_logic(
    input [2:0] current_floor,
    input [7:0] cabin_req,
    input [7:0] hall_up,
    input [7:0] hall_down,
    input Up,
    input Down,
    output reg idle,
    output reg door,
    output reg door_timer
);

    always @(*) begin
        // default values
        idle = 0;
        door = 0;
        door_timer = 0;

        // stopping condition
        if (
            cabin_req[current_floor] ||
            (Up && hall_up[current_floor]) ||
            (Down && hall_down[current_floor])
        ) begin
            idle = 1;
            door = 1;
            door_timer = 1;
        end
    end

endmodule