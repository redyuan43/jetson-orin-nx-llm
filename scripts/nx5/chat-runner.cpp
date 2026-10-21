// Standalone nx5 transport adapter; links the existing engine without editing it.
#include "arg.h"
#include "chat.h"
#include "common.h"
#include "server-context.h"
#include "server-task.h"
#include <atomic>
#include <csignal>
#include <iostream>
#include <thread>

static std::atomic<bool> interrupted{false};
static void interrupt(int) { interrupted.store(true); }
static bool stopped() { return interrupted.load(); }
static void emit(const json & value) { std::cout << value.dump() << std::endl; }

int main(int argc, char ** argv) {
    common_params params;
    common_init();
    if (!common_params_parse(argc, argv, params, LLAMA_EXAMPLE_CLI)) return 1;
    std::signal(SIGINT, interrupt);
    std::signal(SIGTERM, interrupt);
    server_context server;
    std::thread worker;
    int rc = 1;
    try {
        const auto request = json::parse(params.prompt);
        params.prompt.clear();
        const bool enable_thinking = request.value("enable_thinking", false);
        params.enable_reasoning = enable_thinking ? 1 : 0;
        params.default_template_kwargs["enable_thinking"] = enable_thinking ? "true" : "false";
        params.sampling.reasoning_budget_tokens = enable_thinking ? -1 : 0;
        auto messages = common_chat_msgs_parse_oaicompat(request.at("messages"));
        if (request.contains("temperature")) params.sampling.temp = request.at("temperature");
        if (request.contains("top_p")) params.sampling.top_p = request.at("top_p");
        if (request.contains("seed")) params.sampling.seed = request.at("seed");
        if (request.contains("presence_penalty")) params.sampling.penalty_present = request.at("presence_penalty");
        if (request.contains("frequency_penalty")) params.sampling.penalty_freq = request.at("frequency_penalty");
        llama_backend_init();
        llama_numa_init(params.numa);
        if (!server.load_model(params)) throw std::runtime_error("model_load_failed");
        auto meta = server.get_meta();
        const auto & options = meta.chat_params;
        common_chat_templates_inputs inputs;
        inputs.messages = messages;
        inputs.tool_choice = COMMON_CHAT_TOOL_CHOICE_NONE;
        if (request.contains("tools")) {
            auto tools = request.at("tools");
            const auto choice = request.at("tool_choice");
            if (choice.is_object()) {
                const auto name = choice.at("function").at("name");
                json selected = json::array();
                for (const auto & tool : tools) {
                    if (tool.at("function").at("name") == name) selected.push_back(tool);
                }
                tools = std::move(selected);
                inputs.tool_choice = COMMON_CHAT_TOOL_CHOICE_REQUIRED;
            } else {
                inputs.tool_choice = common_chat_tool_choice_parse_oaicompat(choice.get<std::string>());
            }
            inputs.tools = common_chat_tools_parse_oaicompat(tools);
            inputs.parallel_tool_calls = request.value("parallel_tool_calls", true);
        }
        inputs.use_jinja = options.use_jinja;
        inputs.add_generation_prompt = true;
        inputs.enable_thinking = enable_thinking;
        inputs.reasoning_format = COMMON_REASONING_FORMAT_DEEPSEEK;
        inputs.force_pure_content = options.force_pure_content;
        inputs.chat_template_kwargs = options.chat_template_kwargs;
        inputs.chat_template_kwargs["enable_thinking"] = enable_thinking ? "true" : "false";
        auto formatted = common_chat_templates_apply(options.tmpls.get(), inputs);
        auto ctx = server.get_llama_context();
        auto vocab = llama_model_get_vocab(llama_get_model(ctx));
        const auto tokens = common_tokenize(vocab, formatted.prompt, true, true);
        if (tokens.size() + params.n_predict > llama_n_ctx(ctx)) {
            emit({{"type", "error"}, {"code", "context_length_exceeded"}});
            return 2;
        }
        worker = std::thread([&]() { server.start_loop(); });
        {
            auto reader = server.get_response_reader();
            server_task task(SERVER_TASK_TYPE_COMPLETION);
            task.id = reader.get_new_id();
            task.index = 0;
            task.cli = true;
            task.cli_prompt = formatted.prompt;
            task.params.sampling = params.sampling;
            if (!formatted.grammar.empty()) {
                task.params.sampling.grammar = {COMMON_GRAMMAR_TYPE_TOOL_CALLS, formatted.grammar};
                task.params.sampling.grammar_lazy = formatted.grammar_lazy;
                task.params.sampling.generation_prompt = formatted.generation_prompt;
                for (const auto & text : formatted.preserved_tokens) {
                    auto ids = common_tokenize(vocab, text, false, true);
                    if (ids.size() == 1) task.params.sampling.preserved_tokens.insert(ids[0]);
                }
                for (auto trigger : formatted.grammar_triggers) {
                    if (trigger.type == COMMON_GRAMMAR_TRIGGER_TYPE_WORD) {
                        auto ids = common_tokenize(vocab, trigger.value, false, true);
                        if (ids.size() == 1) {
                            trigger.type = COMMON_GRAMMAR_TRIGGER_TYPE_TOKEN;
                            trigger.token = ids[0];
                            task.params.sampling.preserved_tokens.insert(ids[0]);
                        }
                    }
                    task.params.sampling.grammar_triggers.push_back(std::move(trigger));
                }
            }
            task.params.speculative = params.speculative;
            task.params.n_keep = params.n_keep;
            task.params.n_predict = params.n_predict;
            task.params.stream = true;
            task.params.timings_per_token = true;
            task.params.antiprompt = request.value("stop", std::vector<std::string>{});
            task.params.antiprompt.insert(task.params.antiprompt.end(), formatted.additional_stops.begin(), formatted.additional_stops.end());
            task.params.chat_parser_params = common_chat_parser_params(formatted);
            task.params.chat_parser_params.reasoning_format = COMMON_REASONING_FORMAT_DEEPSEEK;
            if (!formatted.parser.empty()) task.params.chat_parser_params.parser.load(formatted.parser);
            if (!formatted.thinking_end_tag.empty()) {
                auto & sampling = task.params.sampling;
                sampling.reasoning_budget_tokens = enable_thinking ? -1 : 0;
                sampling.generation_prompt = formatted.generation_prompt;
                if (!formatted.thinking_start_tag.empty())
                    sampling.reasoning_budget_start = common_tokenize(vocab, formatted.thinking_start_tag, false, true);
                sampling.reasoning_budget_end = common_tokenize(vocab, formatted.thinking_end_tag, false, true);
                sampling.reasoning_budget_forced = common_tokenize(vocab, sampling.reasoning_budget_message + formatted.thinking_end_tag, false, true);
            }
            reader.post_task({std::move(task)});
            std::string content;
            std::string reasoning_content;
            auto emit_diffs = [&](const std::vector<common_chat_msg_diff> & diffs) {
                for (const auto & diff : diffs) {
                    if (!diff.reasoning_content_delta.empty()) {
                        reasoning_content += diff.reasoning_content_delta;
                        emit({{"type", "reasoning_delta"}, {"reasoning_content", diff.reasoning_content_delta}});
                    }
                    if (!diff.content_delta.empty()) {
                        content += diff.content_delta;
                        emit({{"type", "delta"}, {"content", diff.content_delta}});
                    }
                    if (diff.tool_call_index != std::string::npos) {
                        json delta = {{"index", diff.tool_call_index}, {"function", json::object()}};
                        if (!diff.tool_call_delta.id.empty()) {
                            delta["id"] = diff.tool_call_delta.id;
                            delta["type"] = "function";
                        }
                        if (!diff.tool_call_delta.name.empty()) delta["function"]["name"] = diff.tool_call_delta.name;
                        if (!diff.tool_call_delta.arguments.empty()) delta["function"]["arguments"] = diff.tool_call_delta.arguments;
                        emit({{"type", "tool_call_delta"}, {"tool_call", delta}});
                    }
                }
            };
            while (auto result = reader.next(stopped)) {
                if (stopped()) break;
                if (result->is_error()) throw std::runtime_error("engine_error");
                if (auto part = dynamic_cast<server_task_result_cmpl_partial *>(result.get()))
                    emit_diffs(part->oaicompat_msg_diffs);
                if (auto final = dynamic_cast<server_task_result_cmpl_final *>(result.get())) {
                    emit_diffs(final->oaicompat_msg_diffs);
                    auto calls = final->oaicompat_msg.to_json_oaicompat().value("tool_calls", json::array());
                    emit({{"type", "final"}, {"content", content}, {"reasoning_content", reasoning_content},
                          {"tool_calls", calls},
                          {"finish_reason", final->stop == STOP_TYPE_LIMIT ? "length" : calls.empty() ? "stop" : "tool_calls"},
                          {"usage", final->usage_json_oaicompat()}, {"timings", final->timings.to_json()}});
                    rc = 0;
                    break;
                }
            }
        }
    } catch (const std::exception & error) {
        // Detailed engine diagnostics stay in private stderr, not public error bodies.
        std::cerr << error.what() << std::endl;
        emit({{"type", "error"}, {"code", "inference_failed"}});
    }
    if (worker.joinable()) { server.terminate(); worker.join(); }
    if (rc && interrupted.load()) emit({{"type", "error"}, {"code", "interrupted"}});
    return rc;
}
