#include <madrona/tracing.hpp>

#include <unistd.h>
#include <fstream>

#include <cassert>

namespace madrona {

// FIXME: move this into context
thread_local HostTracing HOST_TRACING;

void WriteToFile(void *data, size_t num_bytes,
                 const std::string &file_path,
                 const std::string &name)
{
    std::string file_name = file_path + std::to_string(getpid()) + name + ".bin";
    std::ofstream myFile(file_name, std::ios::out | std::ios::binary);
    myFile.write((char *)data, num_bytes);
    myFile.close();
}

void FinalizeLogging(const std::string file_path)
{
    auto num_events = HOST_TRACING.events.size();
    assert(num_events == HOST_TRACING.time_stamps.size());

    std::vector<int64_t> concat(num_events * 2);

    for (size_t i = 0; i < num_events; i++)
    {
        concat[i] = static_cast<int64_t>(HOST_TRACING.events[i]);
        concat[i + num_events] = HOST_TRACING.time_stamps[i];
    }

    WriteToFile<int64_t>(concat.data(), num_events * 2, file_path, "_madrona_host_tracing");
}

} // namespace madrona
