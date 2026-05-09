`timescale 1ns / 1ps

module request_logic(
    input [7:0] cabin_req,
    input [7:0] hall_up,
    input [7:0] hall_down,
    input [7:0] requests_in,
    output reg [7:0] requests_out,
    output reg [2:0] max_out,
    output reg [2:0] min_out
);

    integer i;

    always @(*) begin
        // Combine all requests
        requests_out = requests_in | cabin_req | hall_up | hall_down;

        // Find max request
        max_out = 0;
        for (i = 0; i < 8; i = i + 1) begin
            if (requests_out[i])
                max_out = i;
        end

        // Find min request
        min_out = 7;
        for (i = 0; i < 8; i = i + 1) begin
            if (requests_out[i] && i < min_out)
                min_out = i;
        end
    end

endmodule