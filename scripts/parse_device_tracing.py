import os
import sys

from PIL import Image, ImageDraw, ImageColor
import pandas as pd

LOG_STEPS = {}
NUM_HIGHLIGHT_NODES = 10
HIDE_SEEK = True


def parse_device_logs(events):
    global LOG_STEPS
    STEP = -1

    def new_step():
        nonlocal STEP
        STEP += 1
        LOG_STEPS[STEP] = {
            "events": {},
            "SMs": {},
            "mapping": {},
            "final_cycles": {},
            "start_timestamp": 0xFFFFFFFFFFFFFFFF,
            "final_timestamp": 0
        }

    for i in range(0, len(events), 32):
        array = events[i:i + 32]

        event = int.from_bytes(array[:4], byteorder='little')
        funcID = int.from_bytes(array[4:8], byteorder='little')
        numInvocations = int.from_bytes(array[8:12], byteorder='little')
        nodeID = int.from_bytes(array[12:16], byteorder='little')
        blockID = int.from_bytes(array[16:20], byteorder='little')
        smID = int.from_bytes(array[20:24], byteorder='little')
        cycleCount = int.from_bytes(array[24:32], byteorder='little')

        if event == 0:
            # beginning of a megakernel
            new_step()
            LOG_STEPS[STEP]["start_timestamp"] = cycleCount

        elif event in [1, 2]:
            if nodeID not in LOG_STEPS[STEP]["mapping"]:
                LOG_STEPS[STEP]["mapping"][nodeID] = (funcID, numInvocations)
            else:
                assert LOG_STEPS[STEP]["mapping"][nodeID] == (funcID,
                                                              numInvocations)

            if nodeID not in LOG_STEPS[STEP]["events"]:
                LOG_STEPS[STEP]["events"][nodeID] = {
                    event: (smID, blockID, cycleCount)
                }
            else:
                assert event not in LOG_STEPS[STEP]["events"][nodeID]
                LOG_STEPS[STEP]["events"][nodeID][event] = (smID, blockID,
                                                            cycleCount)

        elif event in [3, 4]:
            if smID not in LOG_STEPS[STEP]["SMs"]:
                assert event == 3
                LOG_STEPS[STEP]["SMs"][smID] = {
                    (numInvocations, nodeID, blockID): [cycleCount]
                }
            else:
                if (numInvocations, nodeID,
                        blockID) in LOG_STEPS[STEP]["SMs"][smID]:
                    assert event == 4
                    LOG_STEPS[STEP]["SMs"][smID][(numInvocations, nodeID,
                                                  blockID)].append(cycleCount)
                else:
                    LOG_STEPS[STEP]["SMs"][smID][(numInvocations, nodeID,
                                                  blockID)] = [cycleCount]

        elif event == 5:
            assert blockID not in LOG_STEPS[STEP]["final_cycles"]
            LOG_STEPS[STEP]["final_cycles"][blockID] = cycleCount
            LOG_STEPS[STEP]["final_timestamp"] = max(
                LOG_STEPS[STEP]["final_timestamp"], cycleCount)
        else:
            assert (False & "event {} not supported".format(event))

    # drop the last step which might be corrupted
    # del LOG_STEPS[STEP]
    print(
        "At the end, complete traces for {} steps are generated".format(STEP))


def serialized_analysis(step_log, nodes_map):

    def calibrate(timestamp, reference):
        return timestamp[2] - reference

    for i in range(max(step_log["events"]) + 1):
        # skipped nodes
        if i not in step_log["events"]:
            continue
        nodes_map[i] = {
            "nodeID":
            i,
            "funcID":
            step_log["mapping"][i][0],
            "invocations":
            step_log["mapping"][i][1],
            "start":
            calibrate(step_log["events"][i][1], step_log["start_timestamp"]),
            "end":
            calibrate(step_log["events"][i][2], step_log["start_timestamp"]),
            "SM utilization": []
        }
        nodes_map[i][
            "duration (ns)"] = nodes_map[i]["end"] - nodes_map[i]["start"]

    total_exec_time = sum(v["duration (ns)"] for v in nodes_map.values())
    for i in nodes_map:
        nodes_map[i]["percentage (%)"] = nodes_map[i][
            "duration (ns)"] / total_exec_time * 100


def block_analysis(step_log, nodes_map):
    sm_execution = {k: [] for k in step_log["SMs"].keys()}
    block_exec_time = {
        "blocks": {k: {}
                   for k in step_log["SMs"].keys()},  # horizontal
        "nodes": {k: {}
                  for k in step_log["SMs"].keys()},  # vertical
    }

    for sm in step_log["SMs"]:
        for (_, nodeID, blockID), time_stamps in step_log["SMs"][sm].items():
            start = time_stamps[0]
            end = max(time_stamps[1:])
            # confirm clock does proceed within an SM
            assert end >= start
            assert start > step_log["start_timestamp"]

            start, end = [
                i - step_log["start_timestamp"] for i in [start, end]
            ]
            sm_execution[sm].append((start, end))

            if blockID not in block_exec_time["blocks"][sm]:
                block_exec_time["blocks"][sm][blockID] = [(start, end, nodeID)]
            else:
                assert start > block_exec_time["blocks"][sm][blockID][-1][1]
                block_exec_time["blocks"][sm][blockID].append(
                    (start, end, nodeID))

            if nodeID not in block_exec_time["nodes"][sm]:
                block_exec_time["nodes"][sm][nodeID] = []
            block_exec_time["nodes"][sm][nodeID].append((start, end, blockID))

        block_exec_time["blocks"][sm] = {
            k: sorted(v)
            for k, v in block_exec_time["blocks"][sm].items()
        }
        block_exec_time["nodes"][sm] = {
            k: sorted(v)
            for k, v in block_exec_time["nodes"][sm].items()
        }

    for s in sm_execution:
        intervals = []
        for start, end in sm_execution[s]:
            if len(intervals) == 0:
                intervals.append((start, end))
                continue
            p = 0
            while p < len(intervals):
                if start > intervals[p][1]:
                    p += 1
                    continue
                elif end < intervals[p][0]:
                    intervals.insert(p, (start, end))
                    break
                else:
                    intervals[p] = (min(start, intervals[p][0]),
                                    max(end, intervals[p][1]))
                    break
            else:
                intervals.insert(p, (start, end))

        i_pointer = 0
        for k, v in nodes_map.items():
            occupied_time = 0
            while i_pointer < len(intervals):
                i_start, i_end = intervals[i_pointer]
                if v["end"] < i_start:
                    break
                elif v["start"] <= i_start and v["end"] >= i_end:
                    occupied_time += i_end - i_start
                    i_pointer += 1
                    continue
                else:
                    assert (False and "no intersections")
            nodes_map[k]["SM utilization"].append(
                occupied_time / nodes_map[k]["duration (ns)"])

    for i in nodes_map:
        assert (len(nodes_map[i]["SM utilization"]) == len(sm_execution))
        nodes_map[i]["SM utilization"] = sum(
            nodes_map[i]["SM utilization"]) / len(sm_execution)

    print(
        "For each SM on average, {:.3f}% of the time there is at least one block is running on"
        .format(
            sum(v["SM utilization"] * v["percentage (%)"]
                for v in nodes_map.values())))

    return block_exec_time


COLORS = [
    "blue", "orange", "red", "green", "purple", "cyan", "pink", "magenta",
    "olive", "navy", "teal", "maroon", "yellow", "black"
]


def plot_events(step_log, nodes_map, blocks, file_name):
    num_sm = len(blocks)
    num_block_per_sm = 1
    num_pixel_per_block = 12
    sm_interval_pixel = num_pixel_per_block // 2
    num_pixel_per_sm = num_block_per_sm * num_pixel_per_block + sm_interval_pixel
    y_blank = num_pixel_per_block * 40
    y_limit = num_sm * num_pixel_per_sm + y_blank
    x_limit = y_limit * 2
    print(x_limit, y_limit)

    top_nodes = sorted([
        i[0] for i in sorted(nodes_map.items(),
                             key=lambda item: item[1]["duration (ns)"])
        [len(nodes_map) - NUM_HIGHLIGHT_NODES:]
    ])

    colors = {}
    if HIDE_SEEK:
        colors = {
            # narrowphase
            28: (0, 76, 153),
            # solvers
            20: (0, 102, 204),
            22: (0, 128, 255),
            24: (102, 178, 255),
            26: (178, 216, 255),
            # broadphase
            30: (199, 31, 102),
            34: (230, 96, 152),
            36: (248, 181, 209),
            # observations
            50: (139, 235, 219),
            52: (90, 184, 168),
            #
            54: (148, 112, 206)
        }
    else:
        for n in top_nodes:
            func = nodes_map[n]["funcID"]
            if func not in colors and len(colors) < len(COLORS):
                colors[func] = COLORS[len(colors)]
    print("Color mapping for functions:", colors)

    img = Image.new("RGB", (x_limit, y_limit), "white")
    draw = ImageDraw.Draw(img)

    def cast_coor(timestamp, limit=x_limit):
        assert (timestamp <= step_log["final_timestamp"])
        return int(timestamp / step_log["final_timestamp"] * limit)

    for s, b in blocks.items():
        y = s * num_pixel_per_sm
        for bb, events in b.items():
            # draw.line((cast_coor(step_log["final_cycles"][bb]), y, x_limit, y),
            #           fill="grey",
            #           width=1)
            for e in events:
                bar_color = colors[nodes_map[e[2]]["funcID"]] if nodes_map[
                    e[2]]["funcID"] in colors else "black"
                bar_color = ImageColor.getrgb(bar_color) if isinstance(
                    bar_color, str) else bar_color
                draw.line((cast_coor(e[0]), y, cast_coor(e[1]), y),
                          fill=bar_color,
                          width=num_pixel_per_block)
                # lighten the first pixel to indicate starting
                draw.line((cast_coor(
                    e[0]), y - num_pixel_per_block // 2 + 1, cast_coor(e[0]),
                           y + num_pixel_per_block - num_pixel_per_block // 2),
                          fill=tuple((i + 255) // 2 for i in bar_color),
                          width=num_pixel_per_block // 3)
            y += sm_interval_pixel

    if not HIDE_SEEK:
        # mark the start and the end of major nodes_map
        y_shift = 0.9
        for n in top_nodes:
            # for n, v in nodes_map.items():
            left, right = cast_coor(nodes_map[n]["start"]), cast_coor(
                nodes_map[n]["end"])
            draw.line((left, 0, left, y_limit), fill="red", width=1)
            draw.line((right, 0, right, y_limit), fill="green", width=1)
            draw.text((left, y_limit - y_blank * y_shift),
                      " f: {}\n t: {:.3f}ms\n {:.1f}%".format(
                          nodes_map[n]["funcID"],
                          (nodes_map[n]["duration (ns)"]) / 1000000,
                          nodes_map[n]["percentage (%)"]),
                      fill=(0, 0, 0))
            y_shift = 1.3 - y_shift

    img.save(file_name)


def step_analysis(step, file_name, tabular_data):
    step_log = LOG_STEPS[step]
    # manually add the nodeStart event for node 0
    step_log["events"][0][1] = (0, 0, step_log["start_timestamp"])

    nodes_map = {}
    serialized_analysis(step_log, nodes_map)

    block_exec_time = block_analysis(step_log, nodes_map)
    plot_events(step_log, nodes_map, block_exec_time["blocks"], file_name)

    for n in nodes_map:
        tabular_data = pd.concat([
            tabular_data,
            pd.DataFrame({k: [v]
                          for k, v in nodes_map[n].items()})
        ])
    return tabular_data


if __name__ == "__main__":
    if len(sys.argv) > 5:
        print(
            "python parse_device_tracing.py [log_name] [# stps, default 1] [start from, default 10] [# highlight nodes, default 10]"
        )
        exit()

    with open(sys.argv[1], 'rb') as f:
        events = bytearray(f.read())
        assert len(events) % 32 == 0
        print("{} events were logged in total".format(len(events) // 32))
        parse_device_logs(events)

    # default value
    steps = 5
    start_from = 10
    if len(sys.argv) >= 3:
        steps = int(sys.argv[2])
    if len(sys.argv) >= 4:
        start_from = int(sys.argv[3])
    if len(sys.argv) >= 5:
        NUM_HIGHLIGHT_NODES = int(sys.argv[4])

    for s in LOG_STEPS:
        LOG_STEPS[s]["final_timestamp"] -= LOG_STEPS[s]["start_timestamp"]
        for b in LOG_STEPS[s]["final_cycles"]:
            LOG_STEPS[s]["final_cycles"][b] -= LOG_STEPS[s]["start_timestamp"]

    dir_path = sys.argv[1] + "_megakernel_events"
    isExist = os.path.exists(dir_path)
    if not isExist:
        os.mkdir(dir_path)
    # todo: limit
    assert start_from < len(LOG_STEPS)
    end_at = min(start_from + steps, len(LOG_STEPS))

    tabular_data = [
        pd.DataFrame({
            "nodeID": [],
            "funcID": [],
            "duration (ns)": [],
            "invocations": [],
            "percentage (%)": [],
            "SM utilization": []
        }) for _ in range(start_from, end_at)
    ]

    with pd.ExcelWriter(dir_path + "/metrics.xlsx") as writer:
        for s in range(start_from, end_at):
            step_analysis(s, dir_path + "/step{}.png".format(s),
                          tabular_data[s - start_from]).to_excel(
                              writer,
                              sheet_name="step{}".format(s),
                              index=False)
